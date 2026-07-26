"""Реестр страниц.

Роутер и sitemap строятся отсюда. Порядок в ``PAGES`` определяет и порядок в
sitemap.xml — сверху то, что важнее для индексации, дальше по убыванию.

Группировка отражает воронку: инструменты и продающие страницы, затем
процедурные («как подать», «какой штраф»), затем сегментные («для ИП», «для
ООО»), затем обязательные юридические страницы самого сайта.
"""

from __future__ import annotations

from . import (
    cookie,
    home,
    hub,
    komplekt,
    legal_kontakty,
    legal_oferta,
    legal_privacy,
    legal_vozvrat,
    obrazec_politiki,
    otzyv_soglasiya,
    politika_lending,
    politika_ooo,
    politika_prilozhenie,
    politika_samozanyatyy,
    prikaz_otvetstvennyy,
    razmeshchenie_politiki,
    segment_bot,
    segment_ip,
    segment_shop,
    shtraf_rkn,
    shtrafy,
    soglasie,
    soglasie_rassylka,
    uvedomlenie,
    zhurnal_obrashcheniy,
)
from .base import Page, PageContext  # noqa: F401 - переэкспорт для страниц

PAGES: tuple[Page, ...] = (
    # Инструмент и деньги
    home.PAGE,
    komplekt.PAGE,
    # Крупные информационные кластеры
    obrazec_politiki.PAGE,
    uvedomlenie.PAGE,
    shtrafy.PAGE,
    hub.PAGE,
    soglasie.PAGE,
    cookie.PAGE,
    # Документы комплекта, вынесенные отдельными посадочными
    prikaz_otvetstvennyy.PAGE,
    otzyv_soglasiya.PAGE,
    soglasie_rassylka.PAGE,
    zhurnal_obrashcheniy.PAGE,
    # Процедурные уточнения
    shtraf_rkn.PAGE,
    razmeshchenie_politiki.PAGE,
    # Сегменты аудитории
    segment_ip.PAGE,
    politika_ooo.PAGE,
    segment_shop.PAGE,
    segment_bot.PAGE,
    politika_prilozhenie.PAGE,
    politika_samozanyatyy.PAGE,
    politika_lending.PAGE,
    # Обязательные страницы самого сайта
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
