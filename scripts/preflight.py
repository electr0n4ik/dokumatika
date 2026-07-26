"""Проверка готовности к продакшену.

Запускается перед первым запуском и после каждой правки ``.env``:

    python3 scripts/preflight.py --env-file /etc/dokumatika/.env

Печатает чек-лист и возвращает ненулевой код, если нашлось хоть одно [FAIL].
Смысл в том, чтобы ошибка конфигурации всплыла на сервере до первого платежа,
а не в виде «человек заплатил, а письмо не ушло».

Что скрипт НЕ делает: не ходит в сеть, ничего не создаёт и не меняет. Это
важно — его часто запускают из-под root, и создание файла базы под root'ом
сломало бы сервис, который работает от пользователя dokumatika.
"""

from __future__ import annotations

import argparse
import os
import pwd
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.config import env_flag, env_str, load_runtime_config, load_site_config  # noqa: E402
from app.robokassa import ALLOWED_HASH_ALGORITHMS, load_robokassa_config  # noqa: E402

OK = "ok"
WARN = "!"
FAIL = "FAIL"

MIN_PYTHON = (3, 10)


@dataclass(frozen=True)
class Check:
    level: str
    title: str
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.checks.append(Check(level, title, detail))

    def ok(self, title: str, detail: str = "") -> None:
        self.add(OK, title, detail)

    def warn(self, title: str, detail: str = "") -> None:
        self.add(WARN, title, detail)

    def fail(self, title: str, detail: str = "") -> None:
        self.add(FAIL, title, detail)

    def count(self, level: str) -> int:
        return sum(1 for check in self.checks if check.level == level)


def load_env_file(path: Path) -> int:
    """Загрузить ``KEY=VALUE`` из .env в окружение процесса.

    Формат тот же, что понимает systemd EnvironmentFile: без подстановок и без
    экранирования. Уже заданные переменные окружения перекрываются файлом —
    проверяем именно то, с чем будет работать сервис.
    """
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1
    return loaded


# ------------------------------------------------------------------ проверки


def check_python(report: Report) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= MIN_PYTHON:
        report.ok(f"Python {version}")
    else:
        report.fail(
            f"Python {version} — проект рассчитан на {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+",
            "Обновите Python или возьмите Debian 12 / Ubuntu 22.04+.",
        )


def check_site(report: Report) -> None:
    site = load_site_config()
    if not site.domain:
        report.fail("Домен не задан", "SITE_DOMAIN")
    elif "." not in site.domain or site.domain.startswith("www."):
        report.fail(f"Домен выглядит неправильно: {site.domain}", "Каноническим должен быть apex без www")
    else:
        report.ok(f"Домен: {site.origin}")

    if site.scheme != "https":
        report.fail(
            f"Схема сайта {site.scheme}, а нужна https",
            "По http Robokassa не подключит магазин, а ссылки в письмах будут небезопасными",
        )
    else:
        report.ok("Схема https")


def check_seller(report: Report) -> None:
    site = load_site_config()
    seller = site.seller
    missing = [
        name
        for name, value in (
            ("SELLER_LEGAL_FORM", seller.legal_form),
            ("SELLER_NAME", seller.name),
            ("SELLER_INN", seller.inn),
            ("SELLER_EMAIL", seller.email),
        )
        if not value
    ]
    if missing:
        report.fail(
            "Реквизиты продавца заполнены не полностью",
            "Не заданы: " + ", ".join(missing) + ". Robokassa требует реквизиты в подвале сайта",
        )
    else:
        report.ok(f"Реквизиты продавца: {seller.display_name}, ИНН {seller.inn}")

    digits = "".join(char for char in seller.inn if char.isdigit())
    if seller.inn and (len(digits) not in {10, 12} or digits != seller.inn):
        report.warn(
            f"ИНН «{seller.inn}» не похож на настоящий",
            "10 цифр у юрлица, 12 у ИП и самозанятого, без пробелов и дефисов",
        )

    if not seller.address:
        report.warn("SELLER_ADDRESS не задан", "В подвале ожидается хотя бы город")

    email = site.support_email or seller.email
    if not email or "@" not in email:
        report.fail("Контактный e-mail не задан", "SUPPORT_EMAIL или SELLER_EMAIL")
    else:
        report.ok(f"Контакт для покупателей: {email}")


def check_robokassa(report: Report) -> None:
    config = load_robokassa_config()
    if config is None:
        missing = [
            name
            for name in ("ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2")
            if not env_str(name)
        ]
        report.fail("Robokassa не настроена", "Не заданы: " + ", ".join(missing))
        return

    report.ok(f"Robokassa: магазин {config.merchant_login}")

    if config.test_mode:
        report.fail(
            "Включён тестовый режим Robokassa (ROBOKASSA_TEST_MODE)",
            "Боевые платежи не пройдут, а сверка через OpStateExt тестовые операции не видит",
        )
    else:
        report.ok("Тестовый режим выключен — платежи боевые")

    if config.hash_algorithm not in ALLOWED_HASH_ALGORITHMS:
        report.fail(f"Неизвестный алгоритм подписи: {config.hash_algorithm}")
    elif config.hash_algorithm == "md5":
        report.warn(
            "Алгоритм подписи md5",
            "Работает, но в кабинете Robokassa лучше переключить магазин на sha256",
        )
    else:
        report.ok(f"Алгоритм подписи: {config.hash_algorithm}")
    report.warn(
        "Сверьте алгоритм подписи с кабинетом Robokassa вручную",
        "Расхождение даёт ошибку 29 на платёжной странице и никак иначе не проявляется",
    )

    if config.receipt_tax != "none":
        report.warn(
            f"В чеке ставка НДС «{config.receipt_tax}»",
            "Самозанятый НДС не платит — обычно нужен ROBOKASSA_RECEIPT_TAX=none",
        )
    else:
        report.ok("Чек: без НДС (режим самозанятого)")

    if not env_flag("PAYMENTS_ENABLED", default=True):
        report.warn("Приём платежей выключен рубильником PAYMENTS_ENABLED", "Продаж не будет")
    else:
        report.ok("Приём платежей включён")


def check_runtime(report: Report) -> None:
    runtime = load_runtime_config()

    if runtime.debug:
        report.fail("Включён APP_DEBUG", "В проде отладочный режим не нужен")
    else:
        report.ok("Отладочный режим выключен")

    if runtime.host != "127.0.0.1":
        report.warn(
            f"Приложение слушает {runtime.host}",
            "Наружу должен смотреть только nginx; ожидается 127.0.0.1",
        )
    else:
        report.ok(f"Слушаем {runtime.host}:{runtime.port}")

    token = runtime.admin_token
    if not token:
        report.fail("ADMIN_TOKEN не задан", "Без него админка /admin/ отключена целиком")
    elif len(token) < 16:
        report.warn(
            f"ADMIN_TOKEN короткий ({len(token)} символов)",
            "Возьмите 32+, например openssl rand -hex 24",
        )
    else:
        report.ok("ADMIN_TOKEN задан")

    if runtime.is_maintenance():
        report.warn(
            "Включён режим обслуживания",
            f"Сайт отдаёт 503. Снять: rm {runtime.maintenance_flag_path}",
        )
    else:
        report.ok("Режим обслуживания выключен")

    if not runtime.asset_version:
        report.warn("ASSET_VERSION пуст", "Кэш статики у посетителей не обновится после правки css")


def check_database(report: Report) -> None:
    """Проверяем каталог базы, а не только файл.

    В режиме WAL SQLite пишет рядом с базой файлы -wal и -shm, поэтому право
    записи нужно на КАТАЛОГ. Права только на файл дают «attempt to write a
    readonly database» при первом же заказе.
    """
    runtime = load_runtime_config()
    path = runtime.database_path
    parent = path.parent

    if not parent.is_dir():
        report.fail(f"Каталог базы не существует: {parent}")
        return

    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix=".preflight-", suffix=".tmp"):
            pass
    except OSError as error:
        report.fail(f"Каталог базы недоступен на запись: {parent}", repr(error))
        return
    report.ok(f"Каталог базы доступен на запись: {parent}")

    if not path.exists():
        report.warn(f"База ещё не создана: {path}", "Появится при первом запуске сервиса — это нормально")
        return

    try:
        # RU: mode=rw, а не обычный connect: обычный создал бы файл, если пути
        # в .env опечатка, и мы бы «успешно» проверили пустышку.
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=5)
    except sqlite3.Error as error:
        report.fail(f"База не открывается на запись: {path}", repr(error))
        return
    try:
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as error:
        report.fail(f"База не читается: {path}", repr(error))
        return
    finally:
        conn.close()

    size_mb = round(path.stat().st_size / (1024 * 1024), 2)
    owner = _owner_name(path)
    report.ok(f"База открыта: {path} ({size_mb} МБ, журнал {journal}, владелец {owner})")

    if journal.lower() != "wal":
        report.warn(f"Журнал базы {journal}, ожидался WAL", "Выставляется на старте приложения")
    missing = {"orders", "order_webhook_events", "funnel_counters"} - tables
    if missing:
        report.warn("В базе нет таблиц: " + ", ".join(sorted(missing)), "Схема создаётся при старте сервиса")


def _owner_name(path: Path) -> str:
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError):
        return str(path.stat().st_uid)


def check_static(report: Report) -> None:
    runtime = load_runtime_config()
    root = runtime.static_root
    if not root.is_dir():
        report.fail(f"Каталог статики не найден: {root}", "STATIC_ROOT")
        return
    styles = root / "styles.css"
    if styles.is_file():
        report.ok(f"Статика на месте: {root}")
    else:
        report.warn(f"В статике нет styles.css: {root}", "Сайт откроется без оформления")


def check_mail(report: Report) -> None:
    smtp = load_runtime_config().smtp
    if not smtp.is_configured:
        report.warn(
            "SMTP не настроен — письма с доступом к документам уходить не будут",
            "Не критично: ссылка на заказ показывается покупателю сразу после оплаты",
        )
        return
    report.ok(f"SMTP: {smtp.host}:{smtp.port}, отправитель {smtp.sender}")
    if not smtp.use_tls:
        report.warn("SMTP без TLS", "Пароль почтового ящика уйдёт по сети открытым текстом")


def check_seo(report: Report) -> None:
    site = load_site_config()
    if site.metrika_id:
        report.ok(f"Яндекс.Метрика: счётчик {site.metrika_id}")
        report.warn(
            "Со счётчиком Метрики нужна ослабленная CSP",
            "См. закомментированный вариант в deployment/nginx/security-headers.conf",
        )
    else:
        report.warn("METRIKA_ID не задан", "Внешней аналитики не будет; своя воронка есть в /admin/")

    key = site.indexnow_key
    if not key:
        report.warn("INDEXNOW_KEY не задан", "Новые страницы будут индексироваться дольше")
    elif not (8 <= len(key) <= 128) or not all(char.isalnum() or char == "-" for char in key):
        report.fail(
            f"INDEXNOW_KEY неверного формата: {key}",
            "Нужно 8-128 символов из латиницы, цифр и дефиса",
        )
    else:
        report.ok(f"IndexNow: ключ задан, файл-подтверждение {site.url('/' + key + '.txt')}")


def check_pages(report: Report) -> None:
    """Смоук-тест страниц: реестр импортируется и юридический минимум на месте."""
    try:
        from app.web.pages import PAGES_BY_PATH
    except Exception as error:  # pragma: no cover - ловим любую поломку импорта
        report.fail("Реестр страниц не импортируется", repr(error))
        return

    report.ok(f"Страниц в реестре: {len(PAGES_BY_PATH)}")
    required = {
        "/oferta/": "оферта (требование Robokassa)",
        "/kontakty/": "контакты (требование Robokassa)",
        "/privacy/": "политика обработки ПД",
        "/vozvrat/": "порядок возврата",
    }
    missing = [f"{path} — {title}" for path, title in required.items() if path not in PAGES_BY_PATH]
    if missing:
        report.warn(
            "Не нашёл обязательные для магазина страницы по ожидаемым адресам",
            "; ".join(missing) + ". Проверьте адреса вручную, если они отличаются",
        )
    else:
        report.ok("Оферта, контакты, политика и возврат на месте")


# -------------------------------------------------------------------- вывод


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русские числительные: 1 копия, 2 копии, 5 копий."""
    tail_two = count % 100
    tail_one = count % 10
    if 11 <= tail_two <= 14:
        return many
    if tail_one == 1:
        return one
    if 2 <= tail_one <= 4:
        return few
    return many


def render(report: Report, *, domain: str) -> None:
    print(f"Готовность к продакшену — {domain}")
    print("=" * 72)
    for check in report.checks:
        marker = f"[{check.level}]".ljust(7)
        print(f"{marker}{check.title}")
        if check.detail:
            print(f"       {check.detail}")
    print("=" * 72)
    warns = report.count(WARN)
    print(
        f"Итого: {report.count(OK)} в порядке, "
        f"{warns} {plural(warns, 'предупреждение', 'предупреждения', 'предупреждений')}, "
        f"{report.count(FAIL)} критично"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка готовности сайта к продакшену")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env, напр. /etc/dokumatika/.env")
    parser.add_argument("--strict", action="store_true", help="считать предупреждения ошибками")
    args = parser.parse_args(argv)

    if args.env_file is not None:
        if not args.env_file.is_file():
            print(f"[{FAIL}] Файл окружения не найден: {args.env_file}")
            return 2
        loaded = load_env_file(args.env_file)
        print(f"Прочитано переменных из {args.env_file}: {loaded}\n")

    report = Report()
    check_python(report)
    check_site(report)
    check_seller(report)
    check_robokassa(report)
    check_runtime(report)
    check_database(report)
    check_static(report)
    check_mail(report)
    check_seo(report)
    check_pages(report)

    render(report, domain=load_site_config().domain or "домен не задан")

    if report.count(FAIL):
        print("\nЗапускать в бой рано: сначала закройте пункты [FAIL].")
        return 1
    if args.strict and report.count(WARN):
        print("\nРежим --strict: предупреждения тоже считаются ошибками.")
        return 1
    print("\nКритичных проблем нет. Предупреждения прочитайте — они не просто так.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
