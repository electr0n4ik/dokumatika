"""Контракт страницы.

Каждая страница — модуль в этом пакете, который объявляет объект ``PAGE``
(или кортеж ``PAGES``). Роутер и sitemap собираются из реестра автоматически,
поэтому добавить страницу = создать файл и вписать его в ``pages/__init__.py``.

Функция сборки получает контекст и возвращает пару «мета для <head>» и «тело».
Она обязана быть чистой: никаких запросов к БД и обращений к сети — иначе
страницы перестанут быть тестируемыми без окружения.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ...config import RuntimeConfig, SiteConfig
from ...products import Product
from ..html import Raw
from ..seo import PageMeta


@dataclass(frozen=True)
class PageContext:
    site: SiteConfig
    runtime: RuntimeConfig
    product: Product
    # RU: Оплата может быть выключена рубильником — страницы обязаны это учитывать
    # и показывать честную заглушку вместо нерабочей кнопки.
    payments_enabled: bool = True

    @property
    def checkout_disabled_note(self) -> str:
        return "Приём оплаты временно приостановлен. Бесплатный генератор работает как обычно."


BuildFn = Callable[[PageContext], "tuple[PageMeta, Raw]"]


@dataclass(frozen=True)
class Page:
    """Одна страница сайта."""

    path: str
    build: BuildFn
    # RU: Параметры для sitemap.xml.
    changefreq: str = "monthly"
    priority: str = "0.7"
    in_sitemap: bool = True
    # RU: Имена файлов из src/static/js, подключаемых внизу страницы.
    scripts: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError(f"page path must start with '/': {self.path}")
        if self.path != "/" and not self.path.endswith("/"):
            raise ValueError(f"page path must end with '/': {self.path}")
