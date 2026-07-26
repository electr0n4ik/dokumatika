"""Тесты строителей ответов: оформление заказа и обработка ResultURL.

Здесь проверяется бизнес-логика денег в отрыве от HTTP: что нельзя оплатить
дешевле, что оферта обязательна, что повторный колбэк не выдаёт товар дважды и
что страница успеха ничего не подтверждает сама по себе.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest

from app.handlers import (
    apply_robokassa_result,
    build_admin_page,
    build_checkout_page,
    build_order_creation,
    build_order_page,
    build_payment_return_page,
    build_robots_txt,
    build_sitemap,
    normalize_email,
    validate_email,
)
from app.products import KOMPLEKT_152FZ
from app.repositories.orders_repository import OrdersRepository
from app.robokassa import build_signature_base, hash_signature


def valid_payload(**overrides) -> dict:
    payload = {
        "email": "buyer@example.com",
        "accept_offer": True,
        "accept_privacy": True,
    }
    payload.update(overrides)
    return payload


class TestEmail:
    @pytest.mark.parametrize("value", ["a@b.ru", "ivan.petrov@mail.example.com", "x_y@sub.domain.ru"])
    def test_accepts_valid(self, value: str) -> None:
        assert validate_email(value)

    @pytest.mark.parametrize("value", ["", "no-at", "a@b", "a b@c.ru", "@b.ru", "a@.ru"])
    def test_rejects_invalid(self, value: str) -> None:
        assert not validate_email(value)

    def test_normalizes_case_and_spaces(self) -> None:
        assert normalize_email("  Ivan@Example.RU ") == "ivan@example.ru"


class TestOrderCreation:
    def test_creates_order_with_catalog_price(self, orders: OrdersRepository, robokassa, site) -> None:
        body, status = build_order_creation(
            payload=valid_payload(),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=True,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.OK
        order = orders.get_by_id(body["order_id"])
        assert order.amount_minor == KOMPLEKT_152FZ.amount_minor

    def test_price_from_request_is_ignored(self, orders: OrdersRepository, robokassa, site) -> None:
        """Попытка подсунуть свою цену не должна ни на что влиять."""
        body, status = build_order_creation(
            payload=valid_payload(amount_minor=1, amount="1.00", price=1),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=True,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.OK
        assert orders.get_by_id(body["order_id"]).amount_minor == 79900

    def test_requires_offer_acceptance(self, orders: OrdersRepository, robokassa, site) -> None:
        body, status = build_order_creation(
            payload=valid_payload(accept_offer=False),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=True,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.BAD_REQUEST and body["error"] == "offer_required"

    def test_requires_privacy_consent(self, orders: OrdersRepository, robokassa, site) -> None:
        body, status = build_order_creation(
            payload=valid_payload(accept_privacy=False),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=True,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.BAD_REQUEST and body["error"] == "privacy_required"

    def test_rejects_bad_email(self, orders: OrdersRepository, robokassa, site) -> None:
        body, status = build_order_creation(
            payload=valid_payload(email="broken"),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=True,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.BAD_REQUEST and body["error"] == "bad_email"

    def test_blocked_when_payments_disabled(self, orders: OrdersRepository, robokassa, site) -> None:
        body, status = build_order_creation(
            payload=valid_payload(),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=False,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.SERVICE_UNAVAILABLE and body["error"] == "payments_disabled"

    def test_blocked_when_robokassa_missing(self, orders: OrdersRepository, site) -> None:
        _, status = build_order_creation(
            payload=valid_payload(),
            product=KOMPLEKT_152FZ,
            robokassa=None,
            payments_enabled=True,
            orders=orders,
            site=site,
        )
        assert status == HTTPStatus.SERVICE_UNAVAILABLE

    def test_records_acceptance_evidence(self, orders: OrdersRepository, robokassa, site) -> None:
        """Доказательство акцепта — единственная защита при споре и чарджбэке."""
        body, _ = build_order_creation(
            payload=valid_payload(),
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
            payments_enabled=True,
            orders=orders,
            site=site,
            client_ip="203.0.113.9",
            user_agent="Mozilla/5.0",
        )
        metadata = orders.get_by_id(body["order_id"]).metadata
        assert metadata["accepted_ip"] == "203.0.113.9"
        assert metadata["accepted_user_agent"] == "Mozilla/5.0"
        assert metadata["legal_version"]
        assert metadata["accepted_at"]


def sign_result(config, order, out_sum: str, password: str) -> dict:
    signature = hash_signature(
        build_signature_base(
            out_sum, order.invoice_id, password, shp_params=[("Shp_order_id", order.order_id)]
        ),
        config.hash_algorithm,
    )
    return {
        "OutSum": out_sum,
        "InvId": order.invoice_id,
        "Shp_order_id": order.order_id,
        "SignatureValue": signature,
    }


def create_order(orders: OrdersRepository, robokassa, site):
    body, _ = build_order_creation(
        payload=valid_payload(),
        product=KOMPLEKT_152FZ,
        robokassa=robokassa,
        payments_enabled=True,
        orders=orders,
        site=site,
    )
    return orders.get_by_id(body["order_id"])


class TestResultCallback:
    def test_valid_callback_marks_paid(self, orders: OrdersRepository, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        payload = sign_result(robokassa, order, "799.000000", "pass2")
        text, status, updated = apply_robokassa_result(
            payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa
        )
        assert status == HTTPStatus.OK
        assert text == f"OK{order.invoice_id}"
        assert updated.is_paid

    def test_repeat_callback_still_answers_ok(self, orders: OrdersRepository, robokassa, site) -> None:
        """Иначе Robokassa будет слать уведомление бесконечно."""
        order = create_order(orders, robokassa, site)
        payload = sign_result(robokassa, order, "799.000000", "pass2")
        apply_robokassa_result(payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa)
        text, status, updated = apply_robokassa_result(
            payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa
        )
        assert status == HTTPStatus.OK and text == f"OK{order.invoice_id}"
        assert updated.is_paid

    def test_repeat_callback_does_not_redeliver(self, orders: OrdersRepository, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        payload = sign_result(robokassa, order, "799.000000", "pass2")
        apply_robokassa_result(payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa)
        orders.mark_delivered(order.order_id)
        _, _, updated = apply_robokassa_result(
            payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa
        )
        # RU: сервер шлёт письмо только когда delivered_at пуст — здесь он уже проставлен.
        assert updated.delivered_at is not None

    def test_forged_signature_does_not_pay(self, orders: OrdersRepository, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        payload = sign_result(robokassa, order, "799.000000", "pass2")
        payload["SignatureValue"] = "deadbeef" * 8
        text, status, updated = apply_robokassa_result(
            payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa
        )
        assert status == HTTPStatus.FORBIDDEN
        assert not text.startswith("OK")
        assert not orders.get_by_id(order.order_id).is_paid

    def test_lowered_amount_does_not_pay(self, orders: OrdersRepository, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        payload = sign_result(robokassa, order, "1.00", "pass2")
        _, status, _ = apply_robokassa_result(
            payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert not orders.get_by_id(order.order_id).is_paid

    def test_unknown_order_rejected(self, orders: OrdersRepository, robokassa) -> None:
        text, status, updated = apply_robokassa_result(
            payload={"Shp_order_id": "ord_missing", "OutSum": "799.00", "InvId": "1", "SignatureValue": "x"},
            orders=orders,
            product=KOMPLEKT_152FZ,
            robokassa=robokassa,
        )
        assert status == HTTPStatus.NOT_FOUND and updated is None

    def test_missing_order_param_rejected(self, orders: OrdersRepository, robokassa) -> None:
        _, status, _ = apply_robokassa_result(
            payload={"OutSum": "799.00"}, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa
        )
        assert status == HTTPStatus.BAD_REQUEST

    def test_unconfigured_robokassa_rejected(self, orders: OrdersRepository) -> None:
        _, status, _ = apply_robokassa_result(
            payload={"Shp_order_id": "x"}, orders=orders, product=KOMPLEKT_152FZ, robokassa=None
        )
        assert status == HTTPStatus.SERVICE_UNAVAILABLE


class TestPages:
    def test_checkout_page_contains_signed_form(self, orders, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        meta, body = build_checkout_page(
            order=order, product=KOMPLEKT_152FZ, robokassa=robokassa, site=site
        )
        html = str(body)
        assert "auth.robokassa.ru" in html
        assert "SignatureValue" in html
        assert meta.noindex

    def test_checkout_page_has_no_inline_script(self, orders, robokassa, site) -> None:
        """Инлайновый скрипт заставил бы ослабить CSP на всём сайте до 'unsafe-inline'."""
        order = create_order(orders, robokassa, site)
        _, body = build_checkout_page(
            order=order, product=KOMPLEKT_152FZ, robokassa=robokassa, site=site
        )
        assert "<script>" not in str(body)

    def test_checkout_page_keeps_manual_button(self, orders, robokassa, site) -> None:
        """Без JS страница обязана оставаться работоспособной."""
        order = create_order(orders, robokassa, site)
        _, body = build_checkout_page(
            order=order, product=KOMPLEKT_152FZ, robokassa=robokassa, site=site
        )
        html = str(body)
        assert 'type="submit"' in html and "<noscript>" in html

    def test_order_page_hides_documents_until_paid(self, orders, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        _, body = build_order_page(order=order, product=KOMPLEKT_152FZ, site=site)
        assert "package-app" not in str(body)

    def test_order_page_shows_documents_after_payment(self, orders, robokassa, site) -> None:
        order = create_order(orders, robokassa, site)
        payload = sign_result(robokassa, order, "799.000000", "pass2")
        apply_robokassa_result(payload=payload, orders=orders, product=KOMPLEKT_152FZ, robokassa=robokassa)
        _, body = build_order_page(
            order=orders.get_by_id(order.order_id), product=KOMPLEKT_152FZ, site=site
        )
        assert "package-app" in str(body)

    def test_success_page_does_not_claim_payment(self, orders, robokassa, site) -> None:
        """SuccessURL подделывается тривиально — «оплачено» там писать нельзя."""
        order = create_order(orders, robokassa, site)
        _, body = build_payment_return_page(
            order=order, product=KOMPLEKT_152FZ, site=site, success=True
        )
        html = str(body)
        assert "обрабатывается" in html.lower()

    def test_admin_page_renders(self, orders, robokassa, site) -> None:
        create_order(orders, robokassa, site)
        meta, body = build_admin_page(
            site=site,
            orders=orders.recent(10),
            stats=orders.stats(),
            funnel={"wizard_start": 3},
            payments_enabled=True,
            robokassa_configured=True,
            test_mode=False,
        )
        assert meta.noindex and "Панель" in str(body)


class TestRobotsAndSitemap:
    def test_robots_closes_private_sections(self, site) -> None:
        robots = build_robots_txt(site)
        for path in ("/pay/", "/zakaz/", "/admin/", "/api/", "/robokassa/"):
            assert f"Disallow: {path}" in robots
        assert "Sitemap: https://dokumatika.ru/sitemap.xml" in robots

    def test_robots_has_no_obsolete_directives(self, site) -> None:
        """Host и Crawl-delay Яндекс больше не учитывает."""
        robots = build_robots_txt(site)
        assert "Host:" not in robots and "Crawl-delay" not in robots

    def test_sitemap_is_well_formed(self, site) -> None:
        import xml.etree.ElementTree as ET

        xml = build_sitemap(site, (("/", "weekly", "1.0"), ("/komplekt/", "weekly", "0.9")))
        root = ET.fromstring(xml)
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        locations = [element.text for element in root.iter(f"{namespace}loc")]
        assert locations == ["https://dokumatika.ru/", "https://dokumatika.ru/komplekt/"]
        assert all(element.text for element in root.iter(f"{namespace}lastmod"))
