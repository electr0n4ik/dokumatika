"""Реестр страниц.

Роутер и sitemap строятся отсюда. Порядок в ``PAGES`` определяет и порядок в
sitemap.xml — сверху то, что важнее для индексации.
"""

from __future__ import annotations

from .base import Page, PageContext  # noqa: F401 - переэкспорт для страниц
from . import (
    cookie,
    home,
    hub,
    komplekt,
    legal_kontakty,
    legal_oferta,
    legal_privacy,
    legal_vozvrat,
    segment_bot,
    segment_ip,
    segment_shop,
    shtrafy,
    soglasie,
    uvedomlenie,
)

PAGES: tuple[Page, ...] = (
    home.PAGE,
    komplekt.PAGE,
    uvedomlenie.PAGE,
    shtrafy.PAGE,
    hub.PAGE,
    soglasie.PAGE,
    cookie.PAGE,
    segment_ip.PAGE,
    segment_shop.PAGE,
    segment_bot.PAGE,
    legal_oferta.PAGE,
    legal_privacy.PAGE,
    legal_vozvrat.PAGE,
    legal_kontakty.PAGE,
)

PAGES_BY_PATH: dict[str, Page] = {page.path: page for page in PAGES}


def sitemap_entries() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (page.path, page.changefreq, page.priority) for page in PAGES if page.in_sitemap
    )
