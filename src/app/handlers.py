"""Строители ответов — вся логика, не зависящая от HTTP.

Приём, унаследованный из plantsChoise: обработчик в ``server.py`` только
разбирает запрос и зовёт функцию отсюда. Функции ниже принимают простые
аргументы и возвращают данные, поэтому весь основной тест приложения обходится
без сокетов, без сети и почти без базы.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from .config import SiteConfig
from .documents.registry import PAID_DOCUMENTS
from .logging_utils import log_event
from .products import Product
from .repositories.orders_repository import Order, OrdersRepository
from .robokassa import (
    RobokassaConfig,
    build_checkout_form,
    build_expiration_date,
    build_receipt,
    new_invoice_id,
    result_ok_response,
    verify_result_callback,
)
from .web.components import callout, legal_note, section
from .web.html import Raw, esc, join
from .web.seo import PageMeta

# RU: Прагматичная проверка email. Цель — отсечь опечатки, а не реализовать RFC:
# письмо с документами всё равно либо дойдёт, либо нет.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

MAX_EMAIL_LENGTH = 254

# RU: Версия текстов оферты и согласия. Фиксируется в заказе — при споре важно
# знать, какую именно редакцию человек принял. Менять при правке /oferta/.
LEGAL_TEXTS_VERSION = "2026-07-26"


# --------------------------------------------------------------- robots/sitemap


def build_robots_txt(site: SiteConfig) -> str:
    """robots.txt. Служебные разделы закрыты, sitemap указан явно.

    Директивы ``Host`` и ``Crawl-delay`` не используются: Яндекс их больше не
    учитывает — зеркало задаётся 301-редиректом, скорость обхода в Вебмастере.
    """
    return "\n".join(
        (
            "User-agent: *",
            "Allow: /",
            "Disallow: /pay/",
            "Disallow: /zakaz/",
            "Disallow: /oplata/",
            "Disallow: /robokassa/",
            "Disallow: /admin/",
            "Disallow: /healthz",
            "Disallow: /api/",
            "Disallow: /*?utm_",
            "",
            f"Sitemap: {site.url('/sitemap.xml')}",
            "",
        )
    )


def build_sitemap(site: SiteConfig, entries: tuple[tuple[str, str, str], ...]) -> str:
    """sitemap.xml: только канонические URL, отдающие 200, с ``lastmod``."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items = []
    for path, changefreq, priority in entries:
        items.append(
            "<url>"
            f"<loc>{esc(site.url(path))}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{esc(changefreq)}</changefreq>"
            f"<priority>{esc(priority)}</priority>"
            "</url>"
        )
    body = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}"
        "</urlset>"
    )


# ------------------------------------------------------------------- заказы


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()[:MAX_EMAIL_LENGTH]


def validate_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value))


def build_order_creation(
    *,
    payload: dict[str, Any],
    product: Product,
    robokassa: RobokassaConfig | None,
    payments_enabled: bool,
    orders: OrdersRepository,
    site: SiteConfig,
    client_ip: str = "",
    user_agent: str = "",
) -> tuple[dict[str, Any], HTTPStatus]:
    """Создать заказ и вернуть ссылку на оплату.

    Сумма берётся ИЗ КАТАЛОГА, а не из запроса — цену подделать нельзя. Факт
    принятия оферты и согласия фиксируется вместе с временем, IP и версией
    текстов: при споре или чарджбэке это единственное доказательство.
    """
    if not payments_enabled or robokassa is None:
        return {"error": "payments_disabled", "message": "Приём оплаты временно приостановлен"}, (
            HTTPStatus.SERVICE_UNAVAILABLE
        )

    email = normalize_email(payload.get("email"))
    if not validate_email(email):
        return {"error": "bad_email", "message": "Проверьте адрес электронной почты"}, HTTPStatus.BAD_REQUEST

    if not bool(payload.get("accept_offer")):
        return (
            {"error": "offer_required", "message": "Нужно принять условия оферты"},
            HTTPStatus.BAD_REQUEST,
        )
    if not bool(payload.get("accept_privacy")):
        return (
            {"error": "privacy_required", "message": "Нужно согласие на обработку персональных данных"},
            HTTPStatus.BAD_REQUEST,
        )

    is_test = bool(robokassa.test_mode)
    invoice_id = new_invoice_id()
    order = orders.create_order(
        product_code=product.code,
        amount_minor=product.amount_minor,
        email=email,
        invoice_id=invoice_id,
        is_test=is_test,
        metadata={
            "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "accepted_ip": client_ip,
            "accepted_user_agent": user_agent,
            "legal_version": LEGAL_TEXTS_VERSION,
            "accept_marketing": bool(payload.get("accept_marketing")),
        },
    )
    log_event(
        "order_created",
        order_id=order.order_id,
        invoice_id=invoice_id,
        amount_minor=order.amount_minor,
        test_mode=is_test,
        email=email,
    )
    return (
        {
            "status": "ok",
            "order_id": order.order_id,
            "pay_url": f"/pay/{order.access_token}/",
            "order_url": f"/zakaz/{order.access_token}/",
            "amount": product.price_label,
        },
        HTTPStatus.OK,
    )


def apply_robokassa_result(
    *,
    payload: dict[str, str],
    orders: OrdersRepository,
    product: Product,
    robokassa: RobokassaConfig | None,
) -> tuple[str, HTTPStatus, Order | None]:
    """Обработать ResultURL. Возвращает тело ответа, статус и заказ.

    Порядок проверок принципиален: сначала находим заказ, потом сверяем сумму и
    инвойс С НАШЕЙ записью, и только затем считаем подпись. Ни одно значение из
    запроса не принимается на веру.
    """
    if robokassa is None:
        return "robokassa is not configured", HTTPStatus.SERVICE_UNAVAILABLE, None

    order_id = str(payload.get("Shp_order_id") or "").strip()
    if not order_id:
        return "missing order", HTTPStatus.BAD_REQUEST, None

    order = orders.get_by_id(order_id)
    if order is None:
        return "order not found", HTTPStatus.NOT_FOUND, None

    verification = verify_result_callback(
        payload,
        config=robokassa,
        expected_amount_minor=order.amount_minor,
        expected_invoice_id=order.invoice_id,
        is_test=order.is_test,
    )
    if not verification.ok:
        log_event(
            "robokassa_result_rejected",
            order_id=order.order_id,
            reason=verification.reason,
            invoice_id=verification.invoice_id,
        )
        status = (
            HTTPStatus.FORBIDDEN
            if verification.reason in {"invalid_signature", "password_missing"}
            else HTTPStatus.BAD_REQUEST
        )
        return verification.reason, status, order

    event_id = f"robokassa:{order.order_id}:{order.invoice_id}"
    updated, applied = orders.apply_paid_callback(
        event_id=event_id,
        order_id=order.order_id,
        metadata_patch={
            "robokassa_invoice_id": verification.invoice_id,
            "robokassa_out_sum": verification.out_sum,
            "robokassa_signature_verified": True,
            "robokassa_fee": str(payload.get("Fee") or ""),
            "robokassa_payment_method": str(payload.get("PaymentMethod") or ""),
        },
    )
    if updated is None:
        return "order not found", HTTPStatus.NOT_FOUND, None

    log_event(
        "robokassa_result_accepted",
        order_id=updated.order_id,
        invoice_id=order.invoice_id,
        applied=applied,
        amount_minor=updated.amount_minor,
        test_mode=updated.is_test,
    )
    # RU: И на повторный колбэк отвечаем OK — иначе Robokassa будет слать его
    # снова и снова, хотя заказ давно оплачен.
    return result_ok_response(order.invoice_id), HTTPStatus.OK, updated


# -------------------------------------------------------------------- страницы


def build_checkout_page(
    *,
    order: Order,
    product: Product,
    robokassa: RobokassaConfig,
    site: SiteConfig,
) -> tuple[PageMeta, Raw]:
    """Переходник на Robokassa: форма отправляется сама, без кнопки.

    Кнопка всё же нарисована — на случай, если JS выключен: тогда человек
    отправит форму руками, а не упрётся в пустую страницу.
    """
    receipt = build_receipt(
        product.title,
        product.amount_minor,
        payment_method=robokassa.receipt_payment_method,
        payment_object=robokassa.receipt_payment_object,
        tax=robokassa.receipt_tax,
    )
    form = build_checkout_form(
        config=robokassa,
        invoice_id=order.invoice_id,
        order_id=order.order_id,
        amount_minor=order.amount_minor,
        description=product.title,
        email=order.email,
        is_test=order.is_test,
        receipt=receipt,
        expiration_date=build_expiration_date(24),
    )
    fields = form["fields"]
    assert isinstance(fields, dict)
    inputs = join(
        [f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">' for name, value in fields.items()]
    )
    test_banner = (
        callout(
            "Тестовый режим",
            "Это тестовый платёж, реальные деньги не спишутся.",
            tone="warn",
        )
        if order.is_test
        else Raw("")
    )
    body = Raw(
        '<section class="checkout">'
        f"{esc(test_banner)}"
        "<h1>Переходим к оплате</h1>"
        f"<p>Заказ на «{esc(product.title)}» — {esc(product.price_label)}.</p>"
        '<form id="robokassa-form" method="POST" '
        f'action="{esc(form["action"])}">{esc(inputs)}'
        '<noscript><p>Нажмите кнопку, чтобы перейти на страницу оплаты.</p></noscript>'
        '<button class="btn btn-p" type="submit">Перейти к оплате</button>'
        "</form>"
        '<p class="muted">Если страница не открылась автоматически — нажмите кнопку выше.</p>'
        # RU: Автосабмит живёт в /js/pay.js, а не инлайном: инлайновый скрипт
        # потребовал бы 'unsafe-inline' в CSP на всём сайте.
        "</section>"
    )
    meta = PageMeta(
        path=f"/pay/{order.access_token}/",
        title="Переход к оплате",
        description="Переход на защищённую страницу оплаты.",
        noindex=True,
    )
    return meta, body


def build_order_page(*, order: Order, product: Product, site: SiteConfig) -> tuple[PageMeta, Raw]:
    """Страница заказа — она же выдача документов после оплаты."""
    if order.is_paid:
        documents = join([f"<li>{esc(document.title)}</li>" for document in PAID_DOCUMENTS])
        save_hint = callout(
            "Сохраните эту ссылку",
            "Она открывает доступ к вашим документам в любой момент.",
            tone="info",
        )
        disclaimer = legal_note(
            "Документы формируются из ваших ответов и являются типовыми. "
            "Это не юридическая консультация."
        )
        mount = (
            f'<div id="package-app" data-order-paid="1" '
            f'data-order-token="{esc(order.access_token)}"></div>'
        )
        body = Raw(
            '<section class="orderpage is-paid">'
            "<h1>Оплата получена</h1>"
            f"<p>Спасибо! Комплект «{esc(product.title)}» доступен ниже. "
            f"Ссылку продублировали на {esc(order.email)} — сохраните её.</p>"
            f"{esc(save_hint)}"
            f'<h2>Что входит</h2><ul class="doclist">{esc(documents)}</ul>'
            f"{mount}"
            f"{esc(disclaimer)}"
            "</section>"
        )
        title = "Ваши документы"
    elif order.status == "canceled":
        body = Raw(
            '<section class="orderpage">'
            "<h1>Заказ отменён</h1>"
            "<p>Оплата не была завершена. Можно оформить заказ заново — это займёт минуту.</p>"
            '<p><a class="btn btn-p" href="/komplekt/">Вернуться к комплекту</a></p>'
            "</section>"
        )
        title = "Заказ отменён"
    else:
        body = Raw(
            '<section class="orderpage" data-order-pending="1">'
            "<h1>Ожидаем оплату</h1>"
            f"<p>Заказ на «{esc(product.title)}» — {esc(product.price_label)}.</p>"
            f'<p><a class="btn btn-p" href="/pay/{esc(order.access_token)}/">Перейти к оплате</a></p>'
            '<p class="muted">Если вы уже оплатили, статус обновится в течение минуты — '
            "обновите страницу.</p>"
            "</section>"
        )
        title = "Заказ ожидает оплаты"

    meta = PageMeta(
        path=f"/zakaz/{order.access_token}/",
        title=title,
        description="Статус заказа и доступ к документам.",
        noindex=True,
    )
    return meta, body


def build_payment_return_page(
    *,
    order: Order | None,
    product: Product,
    site: SiteConfig,
    success: bool,
) -> tuple[PageMeta, Raw]:
    """Страница возврата с платёжной формы.

    При успехе, но ещё не пришедшем колбэке — честное «обрабатывается» с
    автообновлением, а не ложное «оплачено».
    """
    if not success:
        body = Raw(
            '<section class="orderpage">'
            "<h1>Оплата не завершена</h1>"
            "<p>Деньги не списаны. Если что-то пошло не так — попробуйте ещё раз.</p>"
            '<p><a class="btn btn-p" href="/komplekt/">Вернуться к комплекту</a></p>'
            "</section>"
        )
        return (
            PageMeta(
                path="/oplata/otmena/",
                title="Оплата не завершена",
                description="Платёж отменён.",
                noindex=True,
            ),
            body,
        )

    order_link = f"/zakaz/{order.access_token}/" if order is not None else "/"
    body = Raw(
        '<section class="orderpage" data-await-payment="1">'
        "<h1>Платёж обрабатывается</h1>"
        "<p>Банк подтверждает оплату — обычно это занимает несколько секунд. "
        "Страница обновится сама.</p>"
        f'<p><a class="btn btn-s" href="{esc(order_link)}">Открыть заказ</a></p>'
        f'<meta http-equiv="refresh" content="5;url={esc(order_link)}">'
        "</section>"
    )
    return (
        PageMeta(
            path="/oplata/uspeh/",
            title="Платёж обрабатывается",
            description="Ожидаем подтверждение оплаты.",
            noindex=True,
        ),
        body,
    )


def build_admin_login_page(
    *,
    site: SiteConfig,
    error: str = "",
    query_token_seen: bool = False,
) -> tuple[PageMeta, Raw]:
    """Форма входа в админку.

    Токен вводится в поле и уходит POST-ом, а не в адресной строке: query-строка
    целиком ложится в access-лог nginx, в историю браузера и в Referer, а по
    этому токену видны e-mail всех покупателей.
    """
    problem = callout("Не вошли", error, tone="warn") if error else Raw("")
    hint = (
        callout(
            "Токен из ссылки не принимается",
            "Адресная строка попадает в логи и в историю браузера. Вставьте токен в поле ниже.",
            tone="info",
        )
        if query_token_seen
        else Raw("")
    )
    body = Raw(
        '<section class="orderpage">'
        "<h1>Вход в панель</h1>"
        f'<p class="muted">Служебный раздел «{esc(site.brand)}».</p>'
        f"{esc(problem)}{esc(hint)}"
        '<form method="POST" action="/admin/" class="adminlogin">'
        '<p><label for="admin-token">Токен администратора</label></p>'
        '<p><input id="admin-token" name="token" type="password" autocomplete="off" '
        'autocapitalize="off" spellcheck="false" required></p>'
        '<p><button class="btn btn-p" type="submit">Войти</button></p>'
        "</form>"
        "</section>"
    )
    meta = PageMeta(path="/admin/", title="Вход в панель", description="Служебная страница.", noindex=True)
    return meta, body


def build_admin_page(
    *,
    site: SiteConfig,
    orders: list[Order],
    stats: dict[str, Any],
    funnel: dict[str, int],
    payments_enabled: bool,
    robokassa_configured: bool,
    test_mode: bool,
) -> tuple[PageMeta, Raw]:
    """Сводка для владельца: деньги, заказы, воронка, состояние рубильников."""
    paid_amount = stats.get("paid_amount_minor", 0) // 100
    test_amount = stats.get("test_paid_amount_minor", 0) // 100
    test_count = stats.get("test_paid_count", 0)
    # RU: Забытый ROBOKASSA_TEST_MODE=1 раздаёт комплект бесплатно, поэтому баннер
    # висит вверху страницы, а не прячется строкой в списке рубильников.
    test_banner = (
        callout(
            "Включён тестовый режим Robokassa",
            "Деньги не списываются, комплект выдаётся бесплатно. Для боевого приёма оплаты "
            "уберите ROBOKASSA_TEST_MODE=1 и перезапустите сервис.",
            tone="warn",
        )
        if test_mode
        else Raw("")
    )
    switches = join(
        [
            f'<li>Приём оплаты: <strong>{"включён" if payments_enabled else "выключен"}</strong></li>',
            f'<li>Robokassa настроена: <strong>{"да" if robokassa_configured else "нет"}</strong></li>',
            f'<li>Режим Robokassa: <strong>{"тестовый" if test_mode else "боевой"}</strong></li>',
        ]
    )
    rows = join(
        [
            "<tr>"
            f"<td>{esc(order.created_at)}</td>"
            f"<td>{esc(order.status)}</td>"
            f"<td>{esc(order.amount_minor // 100)} ₽</td>"
            f"<td>{esc(order.email)}</td>"
            f"<td>{esc(order.invoice_id)}</td>"
            f"<td>{'тест' if order.is_test else 'бой'}</td>"
            "</tr>"
            for order in orders
        ]
    )
    funnel_rows = join(
        [f"<tr><td>{esc(name)}</td><td>{esc(count)}</td></tr>" for name, count in sorted(funnel.items())]
    )
    orders_table = Raw(
        '<div class="table-wrap"><table class="cmp"><thead><tr>'
        "<th>Создан</th><th>Статус</th><th>Сумма</th><th>Email</th><th>InvId</th><th>Режим</th>"
        f"</tr></thead><tbody>{esc(rows)}</tbody></table></div>"
    )
    funnel_table = Raw(
        '<div class="table-wrap"><table class="cmp"><thead><tr>'
        "<th>Событие</th><th>Всего</th>"
        f"</tr></thead><tbody>{esc(funnel_rows)}</tbody></table></div>"
    )
    # RU: Тестовые оплаты — отдельная плитка и явное «не выручка»: 799 ₽ «из
    # воздуха» должны быть видны сразу, а не всплывать при сверке с выпиской.
    test_kpi = (
        f"<div><span>Тестовые оплаты (не выручка)</span>"
        f"<strong>{esc(test_count)} на {esc(test_amount)} ₽</strong></div>"
        if test_count
        else ""
    )
    kpi = Raw(
        '<div class="admin-kpi">'
        f'<div><span>Оплачено заказов (боевые)</span><strong>{esc(stats.get("paid_count", 0))}</strong></div>'
        f"<div><span>Выручка</span><strong>{esc(paid_amount)} ₽</strong></div>"
        f"{test_kpi}"
        "</div>"
    )
    logout = Raw(
        '<form method="POST" action="/admin/" class="adminlogout">'
        '<input type="hidden" name="action" value="logout">'
        '<button class="btn btn-s" type="submit">Выйти</button>'
        "</form>"
    )
    body = Raw(
        "<h1>Панель</h1>"
        f"{esc(test_banner)}"
        f"{esc(kpi)}"
        f"{esc(section('Состояние', Raw(f'<ul>{esc(switches)}</ul>')))}"
        f"{esc(section('Последние заказы', orders_table))}"
        f"{esc(section('Воронка', funnel_table))}"
        f"{esc(logout)}"
    )
    meta = PageMeta(path="/admin/", title="Панель", description="Служебная страница.", noindex=True)
    return meta, body
