"""Собственная воронка событий — независимо от Яндекс.Метрики.

Зачем дублировать Метрику: блокировщики режут её счётчик у заметной доли
аудитории, а решение «усиливать или замораживать проект» принимается по
конверсии. Здесь события считаются на сервере и врать не могут.

Персональных данных не пишем: только тип события, дата и произвольная метка
(например, тип ресурса из визарда). Ни IP, ни User-Agent, ни идентификатора
посетителя — поэтому согласия на такую статистику не требуется.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..db import Database

SCHEMA = """
-- RU: Агрегат, а не журнал: одна строка на (дата, событие, метка). База не
-- растёт от трафика, и в ней принципиально нечего деанонимизировать.
CREATE TABLE IF NOT EXISTS funnel_counters (
    day    TEXT NOT NULL,
    event  TEXT NOT NULL,
    label  TEXT NOT NULL DEFAULT '',
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, event, label)
);
"""

# RU: Белый список событий. Всё, что не отсюда, молча отбрасывается —
# публичный эндпоинт не должен позволять раздувать базу произвольными ключами.
KNOWN_EVENTS = frozenset(
    {
        "page_view",
        "wizard_start",
        "wizard_step",
        "wizard_complete",
        "policy_download",
        "checkout_click",
        "checkout_created",
        "order_paid",
        "package_download",
    }
)

MAX_LABEL_LENGTH = 48

# RU: Метки генерирует наш же клиент и они всегда латинские слаги вроде "step-2".
# Ограничение алфавита отсекает разметку и кавычки, но НЕ ограничивает число
# значений — за это отвечает белый список ниже.
ALLOWED_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.:")

# RU: Метка, под которую сводится всё незнакомое.
OTHER_LABEL = "other"

# RU: Визард из пяти шагов; метки шагов формирует wizard.js как "step-<номер>".
_STEP_LABELS = frozenset(f"step-{number}" for number in range(1, 6))

# RU: Коды документов — метки скачиваний из package.js.
_DOCUMENT_LABELS = frozenset(
    {
        "policy",
        "consent",
        "consent_marketing",
        "cookie_policy",
        "order_responsible",
        "consent_withdrawal",
        "requests_journal",
        "rkn_notice_guide",
    }
)

_FORMAT_LABELS = frozenset({"docx", "html", "print", "copy", "zip"})

# RU: Ключевая защита /api/track: PRIMARY KEY (day, event, label) создаёт строку
# на каждую НОВУЮ метку, поэтому без списка допустимых значений скрипт с одним
# счётчиком в метке пишет по строке на запрос и раздувает базу и WAL. Метки шлёт
# наш же клиент, их конечное число — всё остальное схлопывается в OTHER_LABEL.
# Новый код документа, забытый в этом списке, ничего не ломает: событие всё равно
# сосчитается, просто попадёт в "other".
KNOWN_LABELS: dict[str, frozenset[str]] = {
    "page_view": frozenset(),
    "wizard_start": _STEP_LABELS,
    "wizard_step": _STEP_LABELS,
    "wizard_complete": frozenset({"policy"}),
    "policy_download": _FORMAT_LABELS,
    "checkout_click": frozenset({"komplekt", "checklist"}),
    "checkout_created": frozenset({"komplekt_152fz"}),
    "order_paid": frozenset({"komplekt_152fz"}),
    "package_download": _DOCUMENT_LABELS | _FORMAT_LABELS,
}


@dataclass(frozen=True)
class CounterRow:
    day: str
    event: str
    label: str
    count: int


class MetricsRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def ensure_schema(self) -> None:
        self._db.ensure_schema(SCHEMA)

    @staticmethod
    def normalize(event: str, label: str = "") -> tuple[str, str] | None:
        """Отсеять неизвестное событие и почистить символы метки.

        Кардинальность здесь ещё не ограничена — этим занимается ``resolve``.
        """
        name = str(event or "").strip()
        if name not in KNOWN_EVENTS:
            return None
        lowered = str(label or "").strip().lower()
        clean_label = "".join(char for char in lowered if char in ALLOWED_LABEL_CHARS)
        return name, clean_label[:MAX_LABEL_LENGTH]

    @classmethod
    def resolve(cls, event: str, label: str = "") -> tuple[str, str] | None:
        """Событие и метка в том виде, в каком они лягут в таблицу.

        Незнакомая метка превращается в ``other``: число строк на событие в
        сутки становится конечным, и публичный ``/api/track`` больше не может
        раздувать таблицу уникальными значениями.
        """
        normalized = cls.normalize(event, label)
        if normalized is None:
            return None
        name, clean_label = normalized
        if clean_label and clean_label not in KNOWN_LABELS.get(name, frozenset()):
            return name, OTHER_LABEL
        return name, clean_label

    def track(self, event: str, label: str = "", *, day: str | None = None) -> bool:
        resolved = self.resolve(event, label)
        if resolved is None:
            return False
        name, clean_label = resolved
        current_day = day or datetime.now(timezone.utc).date().isoformat()
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO funnel_counters (day, event, label, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(day, event, label) DO UPDATE SET count = count + 1
                """,
                (current_day, name, clean_label),
            )
        return True

    def totals(self, *, since: date | None = None) -> dict[str, int]:
        query = "SELECT event, SUM(count) AS total FROM funnel_counters"
        params: tuple[object, ...] = ()
        if since is not None:
            query += " WHERE day >= ?"
            params = (since.isoformat(),)
        query += " GROUP BY event"
        with self._db.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return {str(row["event"]): int(row["total"]) for row in rows}

    def daily(self, limit_days: int = 30) -> list[CounterRow]:
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT day, event, label, count FROM funnel_counters
                WHERE day >= date('now', ?)
                ORDER BY day DESC, event ASC
                """,
                (f"-{int(limit_days)} days",),
            ).fetchall()
        return [
            CounterRow(str(row["day"]), str(row["event"]), str(row["label"]), int(row["count"]))
            for row in rows
        ]
