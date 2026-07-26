"""Переиспользуемые блоки страниц.

Набор повторяет SEO-каркас из плана портфеля (Tldr, Faq, Breadcrumbs,
AuthorMeta, LegalNote, ComparisonTable), но собран функциями на Python вместо
Astro-компонентов — сервер рендерит HTML сам, сборщика в проекте нет.
"""

from __future__ import annotations

from ..config import SiteConfig
from .html import Raw, esc, join
from .seo import Crumb, FaqItem


def breadcrumbs(items: tuple[Crumb, ...]) -> Raw:
    if not items:
        return Raw("")
    parts: list[str] = []
    for index, crumb in enumerate(items):
        if crumb.href and index < len(items) - 1:
            parts.append(f'<a href="{esc(crumb.href)}">{esc(crumb.text)}</a>')
        else:
            parts.append(f"<span>{esc(crumb.text)}</span>")
    trail = '<span class="sep" aria-hidden="true">/</span>'.join(parts)
    return Raw(f'<nav class="crumbs" aria-label="Хлебные крошки">{trail}</nav>')


def tldr(content: Raw | str, label: str = "Коротко") -> Raw:
    return Raw(
        f'<div class="tldr"><span class="lbl">{esc(label)}</span>{esc(content)}</div>'
    )


def kicker(text: str) -> Raw:
    return Raw(f'<span class="kicker">{esc(text)}</span>')


def chips(items: tuple[str, ...]) -> Raw:
    if not items:
        return Raw("")
    body = join([f'<span class="chip">{esc(item)}</span>' for item in items])
    return Raw(f'<div class="chips">{esc(body)}</div>')


def faq_block(items: tuple[FaqItem, ...], title: str = "Частые вопросы") -> Raw:
    if not items:
        return Raw("")
    rows = join(
        [
            f"<details class=\"qa\"><summary>{esc(item.question)}</summary>"
            f"<div class=\"qa-body\">{esc(item.answer)}</div></details>"
            for item in items
        ]
    )
    return Raw(
        f'<section class="section" id="faq"><h2>{esc(title)}</h2>'
        f'<div class="qa-list">{esc(rows)}</div></section>'
    )


def author_meta(updated: str, author: str = "Редакция Докуматики") -> Raw:
    """Видимые автор и дата — сигнал E-E-A-T и для Яндекса, и для человека."""
    if not updated:
        return Raw("")
    return Raw(
        f'<div class="authormeta"><span class="mono-label">Обновлено</span>'
        f"<time>{esc(updated)}</time><span class=\"dot\" aria-hidden=\"true\">·</span>"
        f"<span>{esc(author)}</span></div>"
    )


def legal_note(text: str) -> Raw:
    return Raw(
        f'<aside class="legalnote" role="note"><strong>Важно.</strong> {esc(text)}</aside>'
    )


def callout(title: str, body: Raw | str, *, tone: str = "info", href: str = "", cta: str = "") -> Raw:
    link = f'<a class="btn btn-p" href="{esc(href)}">{esc(cta)}</a>' if href and cta else ""
    return Raw(
        f'<div class="callout callout-{esc(tone)}"><div class="callout-text">'
        f"<strong>{esc(title)}</strong><p>{esc(body)}</p></div>{link}</div>"
    )


def tool_card(title: str, body: Raw | str, *, badge: str = "Бесплатно") -> Raw:
    badge_html = f'<span class="free">{esc(badge)}</span>' if badge else ""
    return Raw(
        f'<div class="tool"><div class="toolhead"><span class="t">{esc(title)}</span>'
        f'{badge_html}</div><div class="toolbody">{esc(body)}</div></div>'
    )


def comparison_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], caption: str = "") -> Raw:
    head = join([f"<th scope=\"col\">{esc(item)}</th>" for item in headers])
    body = join(
        [
            "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
        ]
    )
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return Raw(
        f'<div class="table-wrap"><table class="cmp">{cap}<thead><tr>{esc(head)}</tr></thead>'
        f"<tbody>{esc(body)}</tbody></table></div>"
    )


def section(title: str, body: Raw | str, *, anchor: str = "", lead: str = "") -> Raw:
    ident = f' id="{esc(anchor)}"' if anchor else ""
    lead_html = f'<p class="lead">{esc(lead)}</p>' if lead else ""
    return Raw(f'<section class="section"{ident}><h2>{esc(title)}</h2>{lead_html}{esc(body)}</section>')


def steps_list(items: tuple[tuple[str, str], ...]) -> Raw:
    body = join(
        [
            f'<li><h3>{esc(title)}</h3><p>{esc(text)}</p></li>'
            for title, text in items
        ]
    )
    return Raw(f'<ol class="steps">{esc(body)}</ol>')


def cross_links(items: tuple[tuple[str, str, str], ...], title: str = "Читайте дальше") -> Raw:
    """Перелинковка внутри кластера: (заголовок, описание, ссылка)."""
    if not items:
        return Raw("")
    body = join(
        [
            f'<a class="crosslink" href="{esc(href)}"><strong>{esc(head)}</strong>'
            f"<span>{esc(note)}</span></a>"
            for head, note, href in items
        ]
    )
    return Raw(
        f'<section class="section"><h2>{esc(title)}</h2>'
        f'<div class="crosslinks">{esc(body)}</div></section>'
    )


def price_card(
    *,
    title: str,
    price_label: str,
    summary: str,
    includes: tuple[str, ...],
    cta_text: str,
    cta_href: str,
    note: str = "",
    disabled_note: str = "",
) -> Raw:
    items = join([f"<li>{esc(item)}</li>" for item in includes])
    if disabled_note:
        action = f'<p class="price-disabled">{esc(disabled_note)}</p>'
    else:
        action = f'<a class="btn btn-p btn-wide" href="{esc(cta_href)}">{esc(cta_text)}</a>'
    note_html = f'<p class="price-note">{esc(note)}</p>' if note else ""
    return Raw(
        f'<div class="pricecard"><div class="pricehead"><h3>{esc(title)}</h3>'
        f'<div class="price">{esc(price_label)}</div></div>'
        f'<p class="price-summary">{esc(summary)}</p>'
        f'<ul class="price-list">{esc(items)}</ul>{action}{note_html}</div>'
    )


def site_header(site: SiteConfig, *, current_path: str = "") -> Raw:
    def link(text: str, href: str) -> str:
        # RU: aria-current подсвечивает активный пункт и для скринридера, и для CSS.
        current = ' aria-current="page"' if href == current_path else ""
        return f'<a href="{esc(href)}"{current}>{esc(text)}</a>'

    links = join([link(item.text, item.href) for item in site.nav])
    return Raw(
        '<header class="sitehead">'
        f'<a class="brand" href="/"><span class="brandmark">{esc(site.brand_mark)}</span>'
        f'<span><span class="bname">{esc(site.brand)}</span>'
        f'<span class="bnote">{esc(site.brand_note)}</span></span></a>'
        f'<button class="navtoggle" type="button" aria-expanded="false" aria-controls="sitenav" '
        f'aria-label="Меню">☰</button>'
        f'<nav class="sitenav" id="sitenav">{esc(links)}</nav>'
        "</header>"
    )


def site_footer(site: SiteConfig, year: int) -> Raw:
    seller = site.seller
    if seller.is_complete:
        requisites = (
            f"{esc(seller.display_name)}<br>ИНН {esc(seller.inn)}"
            + (f"<br>ОГРНИП {esc(seller.ogrn)}" if seller.ogrn else "")
            + f"<br>{esc(seller.email)}"
        )
    else:
        requisites = '<span class="warn-inline">Реквизиты продавца не заполнены</span>'

    return Raw(
        '<footer class="sitefoot"><div class="shell"><div class="cols">'
        "<div><h5>Инструменты</h5>"
        '<a href="/">Генератор политики</a>'
        '<a href="/komplekt/">Комплект 152-ФЗ</a>'
        '<a href="/soglasie/">Согласие на обработку ПД</a></div>'
        "<div><h5>Разобраться</h5>"
        '<a href="/uvedomlenie-rkn/">Уведомление в Роскомнадзор</a>'
        '<a href="/shtrafy-152-fz/">Штрафы по 152-ФЗ</a>'
        '<a href="/152-fz-dlya-sayta/">152-ФЗ для сайта</a></div>'
        f"<div><h5>Продавец</h5><p class=\"requisites\">{esc(Raw(requisites))}</p></div>"
        "</div><div class=\"bottom\">"
        f"<span>© {esc(year)} {esc(site.brand)}</span>"
        '<a href="/oferta/">Оферта</a>'
        '<a href="/privacy/">Политика конфиденциальности</a>'
        '<a href="/kontakty/">Контакты</a>'
        "</div></div></footer>"
    )
