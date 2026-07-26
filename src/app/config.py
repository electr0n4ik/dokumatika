"""Конфигурация сайта и рантайма.

Единственное место, где читаются переменные окружения. Всё остальное приложение
получает готовые dataclass-объекты — это делает тесты полностью независимыми от env.

Разделение:
- ``SiteConfig``  — «паспорт» сайта: бренд, домен, навигация, реквизиты. Меняется при
  клонировании проекта под новый домен.
- ``RuntimeConfig`` — как запускаемся: порт, база, режим отладки, maintenance.
- ``RobokassaConfig`` живёт в ``app.robokassa`` — он самодостаточный.

Go migration notes:
- Соответствует internal/config; порядок чтения env сохранить.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

# RU: Корень проекта = на два уровня выше этого файла (src/app/config.py -> проект).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = PROJECT_ROOT / "src" / "static"
VAR_ROOT = PROJECT_ROOT / "var"


def env_str(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NavItem:
    text: str
    href: str


@dataclass(frozen=True)
class SellerConfig:
    """Реквизиты продавца — обязательны на сайте, принимающем платежи.

    Пустые значения намеренно допустимы на этапе разработки: страница оферты
    покажет предупреждение вместо выдуманных реквизитов, а ``make preflight``
    свалится с понятной ошибкой (см. scripts/preflight.py).
    """

    legal_form: str = ""  # "Самозанятый" / "ИП" / "ООО"
    name: str = ""  # ФИО или наименование
    inn: str = ""
    ogrn: str = ""
    email: str = ""
    address: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.legal_form and self.name and self.inn and self.email)

    @property
    def display_name(self) -> str:
        if not self.name:
            return "Реквизиты не заполнены"
        if self.legal_form:
            return f"{self.legal_form} {self.name}".strip()
        return self.name


@dataclass(frozen=True)
class SiteConfig:
    """Паспорт сайта. Всё, что отличает dokumatika.ru от следующего клона."""

    brand: str = "Докуматика"
    brand_note: str = "документы по 152-ФЗ"
    brand_mark: str = "Д"
    domain: str = "dokumatika.ru"
    scheme: str = "https"
    title_suffix: str = " — Докуматика"
    default_description: str = (
        "Бесплатный генератор политики конфиденциальности по 152-ФЗ и комплект "
        "документов для сайта: согласие на обработку персональных данных, "
        "уведомление в Роскомнадзор, политика cookie."
    )
    theme: str = "a"
    metrika_id: str = ""
    indexnow_key: str = ""
    support_email: str = ""
    seller: SellerConfig = field(default_factory=SellerConfig)
    nav: tuple[NavItem, ...] = (
        NavItem("Генератор политики", "/"),
        NavItem("Комплект 152-ФЗ", "/komplekt/"),
        NavItem("Уведомление в РКН", "/uvedomlenie-rkn/"),
        NavItem("Штрафы", "/shtrafy-152-fz/"),
    )
    legal_note: str = (
        "Сервис формирует типовые документы по вашим ответам и не является "
        "юридической консультацией."
    )

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.domain}"

    def url(self, path: str = "/") -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.origin}{path}"


@dataclass(frozen=True)
class SmtpConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    use_tls: bool = True

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.sender)


@dataclass(frozen=True)
class RuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    database_path: Path = VAR_ROOT / "dokumatika.sqlite3"
    static_root: Path = STATIC_ROOT
    debug: bool = False
    # RU: Мягкий kill-switch. Включается env-переменной или файлом var/MAINTENANCE.
    maintenance: bool = False
    payments_enabled: bool = True
    # RU: Токен доступа к /admin/. Пустой -> админка выключена целиком.
    admin_token: str = ""
    # RU: Версия статики для cache-busting (?v=...). Меняется при правке css/js.
    asset_version: str = "20260726-3"
    smtp: SmtpConfig = field(default_factory=SmtpConfig)

    @property
    def maintenance_flag_path(self) -> Path:
        return self.database_path.parent / "MAINTENANCE"

    def is_maintenance(self) -> bool:
        """Maintenance активен, если включён env-флагом или создан файл-маркер.

        Файл-маркер удобен в проде: ``touch var/MAINTENANCE`` не требует
        перезапуска сервиса.
        """
        if self.maintenance:
            return True
        try:
            return self.maintenance_flag_path.exists()
        except OSError:
            return False


def load_site_config() -> SiteConfig:
    base = SiteConfig()
    seller = SellerConfig(
        legal_form=env_str("SELLER_LEGAL_FORM"),
        name=env_str("SELLER_NAME"),
        inn=env_str("SELLER_INN"),
        ogrn=env_str("SELLER_OGRN"),
        email=env_str("SELLER_EMAIL"),
        address=env_str("SELLER_ADDRESS"),
    )
    return replace(
        base,
        domain=env_str("SITE_DOMAIN", base.domain),
        scheme=env_str("SITE_SCHEME", base.scheme),
        metrika_id=env_str("METRIKA_ID"),
        indexnow_key=env_str("INDEXNOW_KEY"),
        support_email=env_str("SUPPORT_EMAIL") or seller.email,
        seller=seller,
    )


def load_smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host=env_str("SMTP_HOST"),
        port=env_int("SMTP_PORT", 587),
        user=env_str("SMTP_USER"),
        password=env_str("SMTP_PASSWORD"),
        sender=env_str("SMTP_SENDER") or env_str("SMTP_USER"),
        use_tls=env_flag("SMTP_USE_TLS", default=True),
    )


def load_runtime_config() -> RuntimeConfig:
    raw_db = env_str("DATABASE_PATH")
    database_path = Path(raw_db) if raw_db else VAR_ROOT / "dokumatika.sqlite3"
    return RuntimeConfig(
        host=env_str("APP_HOST", "127.0.0.1"),
        port=env_int("APP_PORT", 8080),
        database_path=database_path,
        static_root=Path(env_str("STATIC_ROOT")) if env_str("STATIC_ROOT") else STATIC_ROOT,
        debug=env_flag("APP_DEBUG", default=False),
        maintenance=env_flag("MAINTENANCE", default=False),
        payments_enabled=env_flag("PAYMENTS_ENABLED", default=True),
        admin_token=env_str("ADMIN_TOKEN"),
        asset_version=env_str("ASSET_VERSION", "20260726-3"),
        smtp=load_smtp_config(),
    )
