"""Слой доступа к SQLite.

Почему SQLite, а не PostgreSQL: проект рассчитан на 2 vCPU / 4 GB, где рядом
живут другие такие же сайты. Отдельный процесс СУБД съел бы 200–300 МБ ни за
что — у нас нагрузка «десятки заказов в месяц» и одна пишущая операция на заказ.
Интерфейс репозиториев намеренно узкий, поэтому переезд на Postgres при росте =
переписать один модуль, не трогая HTTP-слой.

Конкурентность — здесь спрятана главная ловушка. ``ThreadingHTTPServer`` создаёт
**новый поток на каждое соединение** и уничтожает его после. Поэтому привычный
``threading.local()`` для соединений тут работает как утечка: каждое соединение
клиента открывает свой файловый дескриптор SQLite, который уже никто не закроет.
Вместо этого — **пул фиксированного размера** (``LifoQueue``) с соединениями,
созданными с ``check_same_thread=False``, поскольку они кочуют между потоками.

Остальные решения:

* ``journal_mode=WAL`` — единственная прагма, которая живёт в самом файле БД;
  остальные выставляются на каждое соединение.
* ``isolation_level=None`` — отключает автоматические ``BEGIN`` драйвера. Без
  этого Python сам открывает отложенную транзакцию, и апгрейд «читал → пишу»
  даёт ``database is locked``, от которого ``busy_timeout`` НЕ спасает.
* Записи дополнительно сериализуются одним ``Lock``: SQLite всё равно пропускает
  писателей по одному, а так они ждут в очереди Python, а не в цикле повторов.

Go migration notes:
- Соответствует internal/storage/sqlite; набор PRAGMA перенести один в один.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# RU: 5 секунд — эмпирический порог: ниже него в бенчмарках появляются
# «database is locked» на конкурентной записи, выше — прироста уже нет.
BUSY_TIMEOUT_MS = 5000

# RU: 4-6 соединений хватает на 2 vCPU: чтения в WAL идут параллельно, а записи
# SQLite всё равно сериализует.
DEFAULT_POOL_SIZE = 5

# RU: Отрицательное значение cache_size = килобайты (здесь 4 МБ на соединение).
PRAGMAS = (
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-4000",
)


class Database:
    """Пул соединений SQLite и точки входа для транзакций."""

    def __init__(self, path: Path | str, pool_size: int = DEFAULT_POOL_SIZE) -> None:
        self.path = Path(path)
        self._pool_size = max(1, int(pool_size))
        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=self._pool_size)
        self._write_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ пул

    def _new_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        return conn

    def _ensure_initialized(self) -> None:
        """Выставить WAL один раз: прагма пишется в заголовок файла БД."""
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn = self._new_connection()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            finally:
                conn.close()
            for _ in range(self._pool_size):
                self._pool.put(self._new_connection())
            self._initialized = True

    @contextmanager
    def _lease(self) -> Iterator[sqlite3.Connection]:
        """Взять соединение из пула и обязательно вернуть его обратно."""
        self._ensure_initialized()
        conn = self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put(conn)

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break
        self._initialized = False

    # ------------------------------------------------------------- транзакции

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Пишущая транзакция.

        ``BEGIN IMMEDIATE`` берёт write-блокировку сразу, а не при первом UPDATE:
        конфликт обнаруживается до того, как накопится работа на откат.
        """
        with self._write_lock, self._lease() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Чтение вне транзакции — в WAL читатели не мешают писателю."""
        with self._lease() as conn:
            yield conn

    # ----------------------------------------------------------------- схема

    def ensure_schema(self, *statements: str) -> None:
        """Выполнить DDL идемпотентно.

        Миграционного инструмента в проекте нет сознательно: схема маленькая и
        создаётся на старте через ``CREATE TABLE IF NOT EXISTS``. Новая колонка =
        правка ``SCHEMA`` в модуле-владельце таблицы плюс ``add_column_if_missing``.
        """
        with self._write_lock, self._lease() as conn:
            for statement in statements:
                conn.executescript(statement)

    def add_column_if_missing(self, table: str, column: str, ddl: str) -> None:
        """``ALTER TABLE ... ADD COLUMN``, которого нет в SQLite с IF NOT EXISTS."""
        with self._write_lock, self._lease() as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if column not in {str(row["name"]) for row in rows}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ------------------------------------------------------- здоровье и бэкап

    def healthcheck(self) -> dict[str, object]:
        """Проверка для ``/healthz``: реальный запрос, а не «процесс жив»."""
        with self._lease() as conn:
            conn.execute("SELECT 1").fetchone()
        wal = self.path.with_name(self.path.name + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        return {"db": "ok", "wal_mb": round(wal_bytes / (1024 * 1024), 2)}

    def backup_to(self, destination: Path | str) -> Path:
        """Согласованный снимок живой базы без остановки сервиса.

        Копировать файл БД через ``cp`` нельзя: в режиме WAL часть свежих данных
        лежит в ``-wal``, и копия только ``.db`` окажется устаревшей или битой.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lease() as source:
            dest = sqlite3.connect(target)
            try:
                source.backup(dest)
            finally:
                dest.close()
        return target

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """Слить WAL в основной файл и усечь его — для обслуживания по таймеру."""
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("unsupported_checkpoint_mode")
        with self._lease() as conn:
            conn.execute(f"PRAGMA wal_checkpoint({mode})")
