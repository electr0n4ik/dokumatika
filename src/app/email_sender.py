"""Отправка письма с доступом к оплаченным документам.

SMTP не настроен — не беда: ссылка на заказ и так показана покупателю сразу
после оплаты, а письмо лишь дублирует её. Поэтому отсутствие SMTP логируется,
но не считается ошибкой и не мешает выдаче.

Письмо отправляется только текстом: HTML-версия не нужна, зато письмо гарантированно
не попадёт в спам из-за разметки и точно откроется в любом клиенте.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from .config import SiteConfig, SmtpConfig
from .logging_utils import log_event
from .products import Product


def build_order_email(*, site: SiteConfig, product: Product, order: Any) -> EmailMessage:
    order_url = site.url(f"/zakaz/{order.access_token}/")
    lines = [
        "Здравствуйте!",
        "",
        f"Оплата за «{product.title}» получена.",
        "",
        "Ваши документы доступны по ссылке:",
        order_url,
        "",
        "Ссылка постоянная — сохраните это письмо, чтобы вернуться к документам позже.",
        "",
        "Что входит в комплект:",
    ]
    lines.extend(f"— {item}" for item in product.includes)
    lines.extend(
        [
            "",
            "Документы формируются из ваших ответов и являются типовыми.",
            "Это не юридическая консультация.",
            "",
            f"{site.brand} · {site.origin}",
        ]
    )
    if site.support_email:
        lines.append(f"Вопросы: {site.support_email}")

    message = EmailMessage()
    message["Subject"] = f"{product.title} — доступ к документам"
    message["To"] = order.email
    message.set_content("\n".join(lines))
    return message


def send_order_email(*, smtp: SmtpConfig, site: SiteConfig, product: Product, order: Any) -> bool:
    if not smtp.is_configured:
        log_event("order_email_skipped", order_id=order.order_id, reason="smtp_not_configured")
        return False

    message = build_order_email(site=site, product=product, order=order)
    message["From"] = smtp.sender

    with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as client:
        if smtp.use_tls:
            client.starttls()
        if smtp.user and smtp.password:
            client.login(smtp.user, smtp.password)
        client.send_message(message)
    return True
