"""Контакты и реквизиты продавца.

Платёжный сервис требует контакты на видном месте и реквизиты в формате
«Самозанятый ФИО полностью, ИНН, город», поэтому однострочные реквизиты вынесены
отдельным блоком — модератору не придётся их искать.

Второй смысл страницы — честно очертить границы поддержки: мы отвечаем на
вопросы про сервис и оплату, но не консультируем по праву. Лучше сказать это
здесь, чем разбираться в переписке.
"""

from __future__ import annotations

from ...config import SellerConfig
from ..components import (
    author_meta,
    breadcrumbs,
    callout,
    cross_links,
    faq_block,
    legal_note,
    section,
    tldr,
)
from ..html import Raw, esc, join
from ..seo import Crumb, FaqItem, PageMeta
from .base import Page, PageContext

UPDATED = "26.07.2026"

BLANK = "___"

# RU: Собственные сроки ответа. Юридические сроки (10 рабочих дней на претензию и
# на запрос субъекта ПД) живут в /vozvrat/ и /privacy/ — здесь только ссылки.
USUAL_REPLY = "обычно в течение одного рабочего дня"
MAX_REPLY = "3 рабочих дня"


def _value(text: str) -> str:
    return text.strip() or BLANK


def _seller_line(seller: SellerConfig) -> str:
    """Реквизиты одной строкой — формат, который проверяет платёжный сервис."""
    if not seller.is_complete:
        return f"{BLANK}, ИНН {BLANK}, {BLANK}"
    parts = [seller.display_name, f"ИНН {seller.inn}"]
    if seller.address:
        parts.append(seller.address)
    return ", ".join(parts)


def _requisites(seller: SellerConfig) -> Raw:
    rows: list[tuple[str, str]] = [
        ("Продавец", seller.display_name if seller.is_complete else BLANK),
        ("ИНН", _value(seller.inn)),
    ]
    if seller.ogrn:
        rows.append(("ОГРНИП", seller.ogrn))
    rows.append(("Электронная почта", _value(seller.email)))
    rows.append(("Адрес", _value(seller.address)))
    body = join([Raw(f"<div><dt>{esc(name)}</dt><dd>{esc(value)}</dd></div>") for name, value in rows])
    return Raw(f'<dl class="requisites">{esc(body)}</dl>')


def _email_link(email: str) -> Raw:
    if not email:
        return Raw(esc(BLANK))
    return Raw(f'<a href="mailto:{esc(email)}">{esc(email)}</a>')


def _bullets(items: tuple[str, ...]) -> Raw:
    rows = join([Raw(f"<li>{esc(item)}</li>") for item in items])
    return Raw(f'<ul class="doclist">{esc(rows)}</ul>')


def _faq(support: str) -> tuple[FaqItem, ...]:
    return (
        FaqItem(
            "Есть ли телефон поддержки?",
            "Нет, мы работаем только письмом: так у обеих сторон остаётся история переписки, а "
            "ответ можно спокойно проверить по документам.",
        ),
        FaqItem(
            "Как быстро вы отвечаете?",
            f"В рабочие дни — {USUAL_REPLY}. Максимальный срок ответа — {MAX_REPLY}. Заявления о "
            "возврате и запросы по персональным данным рассматриваются в течение 10 рабочих дней.",
        ),
        FaqItem(
            "Вы поможете заполнить документы под мою ситуацию?",
            "Мы отвечаем на вопросы о работе сервиса и составе комплекта, но не оказываем "
            "юридических услуг и не готовим индивидуальные документы. Сервис формирует типовые "
            "шаблоны по вашим ответам.",
        ),
        FaqItem(
            "Куда писать, если оплата прошла, а документы не пришли?",
            f"На {_value(support)} — приложите дату и сумму платежа. Восстановим доступ или вернём "
            "оплату полностью.",
        ),
    )


def build(ctx: PageContext) -> tuple[PageMeta, Raw]:
    site = ctx.site
    seller = site.seller
    support = site.support_email.strip()
    crumbs = (Crumb("Главная", "/"), Crumb("Контакты"))

    warn = Raw("")
    if not seller.is_complete or not support:
        warn = callout(
            "Реквизиты продавца не заполнены",
            "Заполните переменные SELLER_* в .env до подключения приёма оплаты: сейчас вместо "
            "реквизитов и адреса поддержки на странице стоят прочерки.",
            tone="warn",
        )

    contact = callout(
        "Электронная почта поддержки",
        Raw(
            f"Пишите на {esc(_email_link(support))} — это единственный канал связи. "
            f"Отвечаем в рабочие дни, {USUAL_REPLY}."
        ),
        tone="info",
        href=f"mailto:{support}" if support else "",
        cta="Написать письмо" if support else "",
    )

    letter = section(
        title="Что указать в письме",
        body=join(
            [
                Raw(
                    "<p>Чтобы ответить с первого раза, приложите к обращению короткий набор данных — "
                    "искать заказ по одному имени мы не сможем.</p>"
                ),
                _bullets(
                    (
                        "Адрес электронной почты, на который оформлялся заказ.",
                        "Дату и сумму платежа, номер заказа из письма или чека.",
                        "Суть вопроса: что именно не работает или что нужно изменить.",
                        "Скриншот, если на экране видна ошибка.",
                    )
                ),
                Raw(
                    "<p>Письмо лучше отправлять с того же адреса, который указан в заказе: так мы "
                    "убеждаемся, что обращение исходит от покупателя.</p>"
                ),
            ]
        ),
        anchor="pismo",
    )

    scope = section(
        title="С чем поможем",
        body=join(
            [
                _bullets(
                    (
                        "Оплата прошла, а доступ к комплекту не открылся.",
                        "Письмо со ссылкой на документы не пришло.",
                        "Вопросы по составу комплекта и по тому, что входит в цену.",
                        "Возврат оплаты — порядок описан на отдельной странице.",
                        "Запросы по персональным данным: какие данные храним, удаление, отзыв согласия.",
                        "Ошибка или опечатка в тексте шаблона — исправим и обновим документ.",
                        "Технические сбои: генератор не открывается, документ не скачивается.",
                    )
                ),
            ]
        ),
        anchor="pomozhem",
    )

    limits = section(
        title="Чего мы не делаем",
        body=join(
            [
                _bullets(
                    (
                        "Не оказываем юридических услуг и не даём правовых консультаций.",
                        "Не проводим экспертизу ваших документов и сайта.",
                        "Не заполняем документы за вас и не подаём уведомление в Роскомнадзор "
                        "от вашего имени.",
                        "Не сопровождаем проверки контролирующих органов.",
                    )
                ),
                Raw(
                    "<p>Сервис формирует типовые шаблоны по вашим ответам. Насколько шаблон подходит "
                    "конкретной деятельности, решаете вы — при необходимости с юристом.</p>"
                ),
            ]
        ),
        anchor="ne-delaem",
    )

    timing = section(
        title="Время ответа",
        body=join(
            [
                Raw(
                    f"<p>Обращения принимаются круглосуточно, отвечаем в рабочие дни: {USUAL_REPLY}, "
                    f"максимальный срок — {MAX_REPLY}.</p>"
                ),
                Raw(
                    "<p>Заявления о возврате и запросы субъектов персональных данных рассматриваются "
                    'в течение 10 рабочих дней — подробности в разделах '
                    '<a href="/vozvrat/">«Возврат и отказ от услуги»</a> и '
                    '<a href="/privacy/">«Политика конфиденциальности»</a>.</p>'
                ),
                Raw(
                    "<p>Если ответа нет дольше указанного срока, отправьте письмо повторно: оно "
                    "могло попасть в спам.</p>"
                ),
            ]
        ),
        anchor="sroki",
    )

    requisites = section(
        title="Реквизиты продавца",
        body=join(
            [
                Raw(f'<p class="requisites">{esc(_seller_line(seller))}</p>'),
                _requisites(seller),
                Raw(
                    "<p>Продавец действует на основании "
                    '<a href="/oferta/">публичной оферты</a>. Кассовый чек направляется на адрес '
                    "электронной почты, указанный при оформлении заказа.</p>"
                ),
            ]
        ),
        anchor="rekvizity",
    )

    faq = _faq(support)

    body = join(
        [
            breadcrumbs(crumbs),
            Raw("<h1>Контакты</h1>"),
            author_meta(UPDATED),
            warn,
            tldr(
                "Связь только по электронной почте. Пишем ответ в рабочие дни, обычно за один "
                "рабочий день. Ниже — что указать в письме, с чем поможем и реквизиты продавца."
            ),
            contact,
            letter,
            scope,
            limits,
            timing,
            requisites,
            faq_block(faq),
            legal_note(site.legal_note),
            cross_links(
                (
                    ("Публичная оферта", "Условия покупки комплекта документов", "/oferta/"),
                    ("Возврат и отказ от услуги", "Когда вернём деньги и как это оформить", "/vozvrat/"),
                    ("Политика конфиденциальности", "Какие данные мы обрабатываем", "/privacy/"),
                ),
                title="Связанные документы",
            ),
        ]
    )

    meta = PageMeta(
        path="/kontakty/",
        title="Контакты",
        description=(
            "Контакты сервиса Докуматика: адрес поддержки, время ответа, с какими вопросами "
            "обращаться и реквизиты продавца."
        ),
        h1="Контакты",
        updated=UPDATED,
        crumbs=crumbs,
        faq=faq,
    )
    return meta, body


PAGE = Page(path="/kontakty/", build=build, changefreq="yearly", priority="0.3")
