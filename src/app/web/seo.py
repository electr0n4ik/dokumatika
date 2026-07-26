"""SEO-каркас: мета-теги и структурированные данные.

Формат страницы взят из плана портфеля (раздел «формат-победитель 2026»):
H1 = целевой запрос, TL;DR в первых 600–1000 знаках, инструмент выше фолда,
Q&A-чанки, JSON-LD, видимые автор и дата обновления.

Здесь только сборка данных. Разметку рисует ``layout.py``/``components.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import SiteConfig
from .html import Raw, esc, strip_tags


@dataclass(frozen=True)
class Crumb:
    text: str
    href: str = ""


@dataclass(frozen=True)
class FaqItem:
    question: str
    answer: str


@dataclass(frozen=True)
class PageMeta:
    """Всё, что нужно для <head> и структурированных данных одной страницы."""

    path: str
    title: str
    description: str
    h1: str = ""
    updated: str = ""
    noindex: bool = False
    og_type: str = "website"
    crumbs: tuple[Crumb, ...] = ()
    faq: tuple[FaqItem, ...] = ()
    # RU: Дополнительные JSON-LD блоки конкретной страницы.
    extra_schemas: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def full_title(self) -> str:
        return strip_tags(self.title)


def json_ld(schema: dict) -> Raw:
    """Отдать JSON-LD.

    ``</`` внутри строк экранируем — иначе значение вида ``</script>`` закрыло бы
    тег и превратилось в исполняемую разметку.
    """
    payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    return Raw(f'<script type="application/ld+json">{payload}</script>')


def organization_schema(site: SiteConfig) -> dict:
    schema: dict = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": site.brand,
        "url": site.url("/"),
    }
    if site.support_email:
        schema["contactPoint"] = {
            "@type": "ContactPoint",
            "contactType": "customer support",
            "email": site.support_email,
            "availableLanguage": "Russian",
        }
    return schema


def website_schema(site: SiteConfig) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": site.brand,
        "url": site.url("/"),
        "inLanguage": "ru-RU",
    }


def breadcrumbs_schema(site: SiteConfig, crumbs: tuple[Crumb, ...]) -> dict | None:
    if not crumbs:
        return None
    items = []
    for index, crumb in enumerate(crumbs, start=1):
        entry: dict = {"@type": "ListItem", "position": index, "name": strip_tags(crumb.text)}
        if crumb.href:
            entry["item"] = site.url(crumb.href)
        items.append(entry)
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}


def faq_schema(faq: tuple[FaqItem, ...]) -> dict | None:
    if not faq:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": strip_tags(item.question),
                "acceptedAnswer": {"@type": "Answer", "text": strip_tags(item.answer)},
            }
            for item in faq
        ],
    }


def _iso_date(updated: str) -> str:
    """``26.07.2026`` -> ``2026-07-26``. Непонятный формат возвращаем как есть."""
    parts = str(updated or "").split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        day, month, year = parts
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return updated


def web_application_schema(
    site: SiteConfig,
    *,
    path: str,
    name: str,
    description: str,
    updated: str,
    price_minor: int = 0,
    category: str = "BusinessApplication",
) -> dict:
    price = f"{price_minor // 100}" if price_minor else "0"
    schema = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": strip_tags(name),
        "url": site.url(path),
        "description": strip_tags(description),
        "applicationCategory": category,
        "operatingSystem": "Web",
        "inLanguage": "ru-RU",
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "RUB"},
    }
    if updated:
        schema["dateModified"] = _iso_date(updated)
    return schema


def product_schema(
    site: SiteConfig,
    *,
    path: str,
    name: str,
    description: str,
    price_minor: int,
    availability: str = "https://schema.org/InStock",
) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": strip_tags(name),
        "description": strip_tags(description),
        "url": site.url(path),
        "brand": {"@type": "Brand", "name": site.brand},
        "offers": {
            "@type": "Offer",
            "url": site.url(path),
            "price": f"{price_minor // 100}",
            "priceCurrency": "RUB",
            "availability": availability,
            "seller": {"@type": "Organization", "name": site.brand},
        },
    }


def how_to_schema(
    site: SiteConfig,
    *,
    path: str,
    name: str,
    description: str,
    steps: tuple[tuple[str, str], ...],
) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "HowTo",
        "name": strip_tags(name),
        "description": strip_tags(description),
        "url": site.url(path),
        "inLanguage": "ru-RU",
        "step": [
            {"@type": "HowToStep", "position": index, "name": strip_tags(title), "text": strip_tags(text)}
            for index, (title, text) in enumerate(steps, start=1)
        ],
    }


def head_meta(site: SiteConfig, meta: PageMeta) -> Raw:
    """Собрать содержимое <head> без открывающего тега."""
    canonical = site.url(meta.path)
    title = esc(meta.full_title + site.title_suffix)
    description = esc(meta.description)
    robots = '<meta name="robots" content="noindex, nofollow">' if meta.noindex else ""
    return Raw(
        f"<title>{title}</title>"
        f'<meta name="description" content="{description}">'
        f'<link rel="canonical" href="{esc(canonical)}">'
        f"{robots}"
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:description" content="{description}">'
        f'<meta property="og:type" content="{esc(meta.og_type)}">'
        f'<meta property="og:url" content="{esc(canonical)}">'
        f'<meta property="og:site_name" content="{esc(site.brand)}">'
        f'<meta property="og:locale" content="ru_RU">'
        f'<meta name="twitter:card" content="summary">'
    )


def page_schemas(site: SiteConfig, meta: PageMeta) -> tuple[dict, ...]:
    """Все JSON-LD блоки страницы в порядке вывода."""
    schemas: list[dict] = []
    crumbs = breadcrumbs_schema(site, meta.crumbs)
    if crumbs:
        schemas.append(crumbs)
    faq = faq_schema(meta.faq)
    if faq:
        schemas.append(faq)
    schemas.extend(meta.extra_schemas)
    return tuple(schemas)
