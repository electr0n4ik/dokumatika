"""Сквозные тесты HTTP-слоя.

Здесь поднимается настоящий сервер на случайном порту и обстреливается через
urllib. Внешних зависимостей это не требует, зато проверяет то, что юнит-тесты
увидеть не могут: маршрутизацию, коды ответов, заголовки, keep-alive и полный
сценарий «создали заказ -> пришёл ResultURL -> документы открылись».
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest

from app.config import RuntimeConfig, SellerConfig, SiteConfig, SmtpConfig
from app.db import Database
from app.robokassa import RobokassaConfig, build_signature_base, hash_signature
from app.server import AppState, Server, make_handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RunningServer:
    def __init__(self, state: AppState, port: int) -> None:
        self.state = state
        self.base = f"http://127.0.0.1:{port}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> tuple[int, str, dict[str, str]]:
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        # RU: urllib по умолчанию идёт по 302 и превращает POST в GET — для
        # проверки самого редиректа (вход в админку) это надо уметь выключать.
        opener = urllib.request.urlopen
        if not follow_redirects:
            opener = urllib.request.build_opener(NoRedirect()).open
        try:
            with opener(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), dict(error.headers)

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        status, body, _ = self.request(
            path,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            return status, json.loads(body)
        except ValueError:
            return status, {}


@pytest.fixture()
def live_server(tmp_path: Path) -> Iterator[RunningServer]:
    site = SiteConfig(
        domain="dokumatika.ru",
        seller=SellerConfig(
            legal_form="Самозанятый",
            name="Иванов Иван Иванович",
            inn="770123456789",
            email="hello@dokumatika.ru",
            address="Москва",
        ),
        support_email="hello@dokumatika.ru",
    )
    runtime = RuntimeConfig(
        host="127.0.0.1",
        port=0,
        database_path=tmp_path / "server.sqlite3",
        static_root=PROJECT_ROOT / "src" / "static",
        admin_token="admin-secret",
        smtp=SmtpConfig(),
    )
    state = AppState(site=site, runtime=runtime, database=Database(runtime.database_path))
    # RU: Подставляем детерминированный конфиг вместо чтения окружения —
    # тест не должен зависеть от того, что лежит в .env разработчика.
    state.robokassa = RobokassaConfig(
        merchant_login="demo",
        password1="pass1",
        password2="pass2",
        test_password1="tpass1",
        test_password2="tpass2",
        hash_algorithm="sha256",
    )
    state.ensure_schema()

    server = Server(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield RunningServer(state, port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        state.database.close()


class TestServiceEndpoints:
    def test_healthz_reports_ok(self, live_server: RunningServer) -> None:
        """Публичный ответ — только «жив»: подробности см. test_server_hardening."""
        status, body, _ = live_server.request("/healthz")
        payload = json.loads(body)
        assert status == HTTPStatus.OK
        assert payload == {"status": "ok"}

    def test_robots_served(self, live_server: RunningServer) -> None:
        status, body, headers = live_server.request("/robots.txt")
        assert status == HTTPStatus.OK
        assert "Sitemap:" in body
        assert headers["Content-Type"].startswith("text/plain")

    def test_sitemap_served_as_xml(self, live_server: RunningServer) -> None:
        status, body, headers = live_server.request("/sitemap.xml")
        assert status == HTTPStatus.OK
        assert headers["Content-Type"].startswith("application/xml")
        assert "<urlset" in body

    def test_content_length_always_present(self, live_server: RunningServer) -> None:
        """HTTP/1.1 без Content-Length подвешивает клиента."""
        for path in ("/", "/healthz", "/robots.txt", "/komplekt/"):
            _, _, headers = live_server.request(path)
            assert "Content-Length" in headers, path

    def test_wizard_api_returns_contract(self, live_server: RunningServer) -> None:
        status, body, _ = live_server.request("/api/wizard.json")
        payload = json.loads(body)
        assert status == HTTPStatus.OK
        assert len(payload["wizard"]["steps"]) == 5
        assert payload["documents"]["free"] == ["policy"]


class TestPages:
    def test_home_renders_wizard_mount(self, live_server: RunningServer) -> None:
        status, body, _ = live_server.request("/")
        assert status == HTTPStatus.OK
        assert 'id="wizard-app"' in body

    def test_unknown_page_is_404(self, live_server: RunningServer) -> None:
        status, body, _ = live_server.request("/nope/")
        assert status == HTTPStatus.NOT_FOUND
        assert "не найдена" in body.lower()

    def test_missing_slash_redirects(self, live_server: RunningServer) -> None:
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(live_server.base + "/komplekt")
        try:
            response = opener.open(request, timeout=10)
            status, location = response.status, response.headers.get("Location")
        except urllib.error.HTTPError as error:
            status, location = error.code, error.headers.get("Location")
        assert status == HTTPStatus.MOVED_PERMANENTLY
        assert location == "/komplekt/"

    def test_all_registered_pages_return_200(self, live_server: RunningServer) -> None:
        from app.web.pages import PAGES

        for page in PAGES:
            status, _, _ = live_server.request(page.path)
            assert status == HTTPStatus.OK, f"{page.path} -> {status}"


class TestStatic:
    def test_styles_served_with_long_cache_when_versioned(self, live_server: RunningServer) -> None:
        status, _, headers = live_server.request("/styles.css?v=test")
        assert status == HTTPStatus.OK
        assert "immutable" in headers.get("Cache-Control", "")
        assert headers["Content-Type"].startswith("text/css")

    def test_path_traversal_blocked(self, live_server: RunningServer) -> None:
        for attack in ("/../src/app/server.py", "/..%2fapp/config.py", "/js/../../app/db.py"):
            status, _, _ = live_server.request(attack)
            assert status in {HTTPStatus.NOT_FOUND, HTTPStatus.BAD_REQUEST}, attack

    def test_directory_is_not_served(self, live_server: RunningServer) -> None:
        status, _, _ = live_server.request("/js/")
        assert status == HTTPStatus.NOT_FOUND


class TestAdmin:
    def test_requires_token(self, live_server: RunningServer) -> None:
        status, body, _ = live_server.request("/admin/")
        assert status == HTTPStatus.UNAUTHORIZED
        assert "Вход в панель" in body and "Панель</h1>" not in body

    def test_wrong_token_rejected(self, live_server: RunningServer) -> None:
        """Неверный токен из формы — 403 и никакой куки."""
        status, _, headers = live_server.request(
            "/admin/",
            method="POST",
            data=urllib.parse.urlencode({"token": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == HTTPStatus.FORBIDDEN
        assert "Set-Cookie" not in headers

    def test_valid_token_grants_access(self, live_server: RunningServer) -> None:
        """Вход только формой: токен не должен попадать в адресную строку."""
        status, _, headers = live_server.request(
            "/admin/",
            method="POST",
            data=urllib.parse.urlencode({"token": "admin-secret"}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert status == HTTPStatus.FOUND and headers["Location"] == "/admin/"
        cookie = headers["Set-Cookie"].split(";")[0]
        status, body, _ = live_server.request("/admin/", headers={"Cookie": cookie})
        assert status == HTTPStatus.OK and "Панель" in body


class TestTracking:
    def test_known_event_accepted(self, live_server: RunningServer) -> None:
        status, payload = live_server.post_json("/api/track", {"event": "wizard_start"})
        assert status == HTTPStatus.OK and payload["ok"] is True

    def test_unknown_event_rejected(self, live_server: RunningServer) -> None:
        status, payload = live_server.post_json("/api/track", {"event": "spam"})
        assert status == HTTPStatus.BAD_REQUEST and payload["ok"] is False


class TestPurchaseFlow:
    def _create_order(self, live_server: RunningServer) -> dict:
        status, payload = live_server.post_json(
            "/api/order",
            {"email": "buyer@example.com", "accept_offer": True, "accept_privacy": True},
        )
        assert status == HTTPStatus.OK, payload
        return payload

    def _result_payload(self, order) -> dict[str, str]:
        signature = hash_signature(
            build_signature_base(
                "799.000000", order.invoice_id, "pass2", shp_params=[("Shp_order_id", order.order_id)]
            ),
            "sha256",
        )
        return {
            "OutSum": "799.000000",
            "InvId": order.invoice_id,
            "Shp_order_id": order.order_id,
            "SignatureValue": signature,
        }

    def test_full_happy_path(self, live_server: RunningServer) -> None:
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])

        # До оплаты страница заказа не отдаёт документы.
        status, body, _ = live_server.request(created["order_url"])
        assert status == HTTPStatus.OK and "package-app" not in body

        # Переход к оплате: подписанная форма на Robokassa.
        status, body, _ = live_server.request(created["pay_url"])
        assert status == HTTPStatus.OK
        assert "auth.robokassa.ru" in body and "SignatureValue" in body
        # RU: автосабмит подключается файлом — иначе строгий CSP его заблокирует.
        assert "/js/pay.js" in body

        # ResultURL — единственное, что подтверждает оплату.
        query = urllib.parse.urlencode(self._result_payload(order))
        status, body, headers = live_server.request(f"/robokassa/result?{query}")
        assert status == HTTPStatus.OK
        assert body == f"OK{order.invoice_id}"
        assert headers["Content-Type"].startswith("text/plain")

        # Теперь документы доступны.
        status, body, _ = live_server.request(created["order_url"])
        assert status == HTTPStatus.OK and "package-app" in body

    def test_result_accepts_post_form(self, live_server: RunningServer) -> None:
        """Метод колбэка настраивается в кабинете — принимать надо оба."""
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        data = urllib.parse.urlencode(self._result_payload(order)).encode("utf-8")
        status, body, _ = live_server.request(
            "/robokassa/result",
            method="POST",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == HTTPStatus.OK and body == f"OK{order.invoice_id}"

    def test_forged_callback_does_not_unlock(self, live_server: RunningServer) -> None:
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        payload = self._result_payload(order)
        payload["SignatureValue"] = "0" * 64
        query = urllib.parse.urlencode(payload)
        status, body, _ = live_server.request(f"/robokassa/result?{query}")
        assert status == HTTPStatus.FORBIDDEN
        assert not body.startswith("OK")

        status, body, _ = live_server.request(created["order_url"])
        assert "package-app" not in body

    def test_success_url_alone_does_not_unlock(self, live_server: RunningServer) -> None:
        """Открыть SuccessURL руками может кто угодно — товар от этого не выдаётся."""
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        status, body, _ = live_server.request(
            f"/oplata/uspeh/?InvId={order.invoice_id}&Shp_order_id={order.order_id}"
        )
        assert status == HTTPStatus.OK
        assert "обрабатывается" in body.lower()
        assert not live_server.state.orders.get_by_id(order.order_id).is_paid

    def test_repeat_callback_is_idempotent(self, live_server: RunningServer) -> None:
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        query = urllib.parse.urlencode(self._result_payload(order))
        first = live_server.request(f"/robokassa/result?{query}")
        second = live_server.request(f"/robokassa/result?{query}")
        assert first[0] == second[0] == HTTPStatus.OK
        assert first[1] == second[1] == f"OK{order.invoice_id}"

    def test_invalid_email_rejected(self, live_server: RunningServer) -> None:
        status, payload = live_server.post_json(
            "/api/order", {"email": "nope", "accept_offer": True, "accept_privacy": True}
        )
        assert status == HTTPStatus.BAD_REQUEST and payload["error"] == "bad_email"

    def test_unknown_order_token_is_404(self, live_server: RunningServer) -> None:
        status, _, _ = live_server.request("/zakaz/does-not-exist/")
        assert status == HTTPStatus.NOT_FOUND

    def test_public_api_does_not_leak_paid_templates(self, live_server: RunningServer) -> None:
        """Платный комплект и есть товар — открытым API его отдавать нельзя."""
        _, body, _ = live_server.request("/api/wizard.json")
        payload = json.loads(body)
        assert payload["documents"]["templates"], "бесплатный шаблон должен остаться"
        codes = {template["code"] for template in payload["documents"]["templates"]}
        # RU: Список кодов платных документов остаётся — по нему визард рисует
        # чек-лист «что ещё нужно». Утечкой был бы их ТЕКСТ, а не названия.
        assert codes == {"policy"}
        assert payload["documents"]["paid"], "чек-лист платных документов нужен визарду"

    def test_package_api_requires_paid_order(self, live_server: RunningServer) -> None:
        created = self._create_order(live_server)
        token = created["order_url"].strip("/").split("/")[-1]
        status, _, _ = live_server.request(f"/api/package.json?token={token}")
        assert status == HTTPStatus.PAYMENT_REQUIRED

    def test_package_api_rejects_unknown_token(self, live_server: RunningServer) -> None:
        status, _, _ = live_server.request("/api/package.json?token=nope")
        assert status == HTTPStatus.NOT_FOUND

    def test_package_api_opens_after_payment(self, live_server: RunningServer) -> None:
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        query = urllib.parse.urlencode(self._result_payload(order))
        live_server.request(f"/robokassa/result?{query}")
        status, body, _ = live_server.request(f"/api/package.json?token={order.access_token}")
        payload = json.loads(body)
        assert status == HTTPStatus.OK
        assert len(payload["documents"]["templates"]) == 8

    def test_fail_url_does_not_cancel_order(self, live_server: RunningServer) -> None:
        """Неподписанный возврат браузера не имеет права закрывать заказ."""
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        live_server.request(f"/oplata/otmena/?InvId={order.invoice_id}&Shp_order_id={order.order_id}")
        assert live_server.state.orders.get_by_id(order.order_id).status != "canceled"

    def test_payment_after_fail_url_still_succeeds(self, live_server: RunningServer) -> None:
        """Гонка «FailURL опередил ResultURL» не должна стоить нам оплаты."""
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        live_server.request(f"/oplata/otmena/?InvId={order.invoice_id}&Shp_order_id={order.order_id}")
        query = urllib.parse.urlencode(self._result_payload(order))
        status, body, _ = live_server.request(f"/robokassa/result?{query}")
        assert status == HTTPStatus.OK and body == f"OK{order.invoice_id}"
        assert live_server.state.orders.get_by_id(order.order_id).is_paid

    def test_success_url_does_not_leak_token_by_invoice(self, live_server: RunningServer) -> None:
        """InvId — число из адресной строки; по нему нельзя выдавать чужой заказ."""
        created = self._create_order(live_server)
        order = live_server.state.orders.get_by_id(created["order_id"])
        _, body, _ = live_server.request(f"/oplata/uspeh/?InvId={order.invoice_id}")
        assert order.access_token not in body

    def test_oversized_body_rejected(self, live_server: RunningServer) -> None:
        """Один POST не должен уметь съесть память процесса."""
        payload = json.dumps({"email": "a@b.ru", "junk": "x" * 200_000}).encode("utf-8")
        status, _, _ = live_server.request(
            "/api/order", method="POST", data=payload, headers={"Content-Type": "application/json"}
        )
        assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


class TestMaintenance:
    def test_maintenance_flag_serves_503(self, live_server: RunningServer, tmp_path: Path) -> None:
        flag = live_server.state.runtime.maintenance_flag_path
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        try:
            status, body, headers = live_server.request("/")
            assert status == HTTPStatus.SERVICE_UNAVAILABLE
            assert "Retry-After" in headers
            assert "временно недоступен" in body
        finally:
            flag.unlink()

    def test_payment_callback_survives_maintenance(self, live_server: RunningServer) -> None:
        """Иначе деньги спишутся, а заказ останется неоплаченным."""
        status, payload = live_server.post_json(
            "/api/order", {"email": "b@example.com", "accept_offer": True, "accept_privacy": True}
        )
        order = live_server.state.orders.get_by_id(payload["order_id"])
        signature = hash_signature(
            build_signature_base(
                "799.000000", order.invoice_id, "pass2", shp_params=[("Shp_order_id", order.order_id)]
            ),
            "sha256",
        )
        flag = live_server.state.runtime.maintenance_flag_path
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        try:
            query = urllib.parse.urlencode(
                {
                    "OutSum": "799.000000",
                    "InvId": order.invoice_id,
                    "Shp_order_id": order.order_id,
                    "SignatureValue": signature,
                }
            )
            status, body, _ = live_server.request(f"/robokassa/result?{query}")
            assert status == HTTPStatus.OK and body == f"OK{order.invoice_id}"
        finally:
            flag.unlink()

    def test_healthz_survives_maintenance(self, live_server: RunningServer) -> None:
        flag = live_server.state.runtime.maintenance_flag_path
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        try:
            status, _, _ = live_server.request("/healthz")
            assert status == HTTPStatus.OK
        finally:
            flag.unlink()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None
