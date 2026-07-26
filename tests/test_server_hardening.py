"""Тесты защитных правок HTTP-слоя.

Каждый тест здесь падал бы до соответствующей правки: вход в админку по
query-строке, подробный /healthz наружу, английская страница 501 на PUT и поток,
навсегда зависший в ``rfile.read()``.

Сервер поднимается свой, а не общий из ``test_server.py``: части тестов нужен
обработчик с укороченным таймаутом, а фикстуре — конфиг Robokassa в тестовом
режиме.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path

import pytest

from app.config import RuntimeConfig, SellerConfig, SiteConfig, SmtpConfig
from app.db import Database
from app.robokassa import RobokassaConfig
from app.server import ADMIN_COOKIE_NAME, AppHandler, AppState, Server, make_handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMIN_TOKEN = "admin-secret"
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class Client:
    """Минимальный HTTP-клиент: без cookie jar, куки передаём руками."""

    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, dict[str, str]]:
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), dict(error.headers)

    def login(self, token: str) -> tuple[int, dict[str, str]]:
        status, _, headers = self.request(
            "/admin/",
            method="POST",
            data=urllib.parse.urlencode({"token": token}).encode("utf-8"),
            headers=dict(FORM_HEADERS),
        )
        return status, headers


def build_state(tmp_path: Path, *, admin_token: str = ADMIN_TOKEN, test_mode: bool = False) -> AppState:
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
        database_path=tmp_path / "hardening.sqlite3",
        static_root=PROJECT_ROOT / "src" / "static",
        admin_token=admin_token,
        smtp=SmtpConfig(),
    )
    state = AppState(site=site, runtime=runtime, database=Database(runtime.database_path))
    state.robokassa = RobokassaConfig(
        merchant_login="demo",
        password1="pass1",
        password2="pass2",
        test_password1="tpass1",
        test_password2="tpass2",
        test_mode=test_mode,
        hash_algorithm="sha256",
    )
    state.ensure_schema()
    return state


@contextmanager
def running(state: AppState, *, handler_timeout: float | None = None) -> Iterator[int]:
    handler_cls = make_handler(state)
    if handler_timeout is not None:
        handler_cls = type(
            "FastTimeoutHandler",
            (handler_cls,),
            {"timeout": handler_timeout, "request_timeout": handler_timeout},
        )
    server = Server(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[Client]:
    state = build_state(tmp_path)
    try:
        with running(state) as port:
            yield Client(port)
    finally:
        state.database.close()


class TestAdminLogin:
    def test_query_token_no_longer_authorizes(self, client: Client) -> None:
        """Токен в адресной строке оседает в логах nginx — доступ он больше не даёт."""
        status, body, _ = client.request(f"/admin/?token={ADMIN_TOKEN}")
        assert status == HTTPStatus.UNAUTHORIZED
        assert "Вход в панель" in body
        assert "Последние заказы" not in body

    def test_query_token_gets_explanation(self, client: Client) -> None:
        _, body, _ = client.request(f"/admin/?token={ADMIN_TOKEN}")
        assert "Токен из ссылки не принимается" in body

    def test_login_sets_hardened_cookie_and_clean_redirect(self, client: Client) -> None:
        status, headers = client.login(ADMIN_TOKEN)
        assert status == HTTPStatus.FOUND
        assert headers["Location"] == "/admin/"
        cookie = headers["Set-Cookie"]
        assert cookie.startswith(f"{ADMIN_COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/admin/" in cookie

    def test_cookie_opens_panel(self, client: Client) -> None:
        _, headers = client.login(ADMIN_TOKEN)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, body, _ = client.request("/admin/", headers={"Cookie": cookie})
        assert status == HTTPStatus.OK
        assert "Панель" in body and "Последние заказы" in body

    def test_session_id_in_cookie_is_not_the_admin_token(self, client: Client) -> None:
        """Утечка куки не должна раскрывать сам ADMIN_TOKEN."""
        _, headers = client.login(ADMIN_TOKEN)
        assert ADMIN_TOKEN not in headers["Set-Cookie"]

    def test_forged_cookie_rejected(self, client: Client) -> None:
        status, _, _ = client.request(
            "/admin/", headers={"Cookie": f"{ADMIN_COOKIE_NAME}=not-a-real-session"}
        )
        assert status == HTTPStatus.UNAUTHORIZED

    def test_wrong_token_is_forbidden_without_cookie(self, client: Client) -> None:
        status, headers = client.login("wrong")
        assert status == HTTPStatus.FORBIDDEN
        assert "Set-Cookie" not in headers

    def test_non_ascii_token_does_not_crash(self, client: Client) -> None:
        """compare_digest на str с кириллицей кидал TypeError -> 500 вместо 403."""
        status, _ = client.login("фыва")
        assert status == HTTPStatus.FORBIDDEN

    def test_header_token_still_works(self, client: Client) -> None:
        """Заголовок оставлен для curl и мониторинга: он не попадает в access-лог."""
        status, body, _ = client.request("/admin/", headers={"X-Admin-Token": ADMIN_TOKEN})
        assert status == HTTPStatus.OK and "Панель" in body

    def test_logout_invalidates_session(self, client: Client) -> None:
        _, headers = client.login(ADMIN_TOKEN)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, out = client.request(
            "/admin/",
            method="POST",
            data=urllib.parse.urlencode({"action": "logout"}).encode("utf-8"),
            headers={**FORM_HEADERS, "Cookie": cookie},
        )
        assert status == HTTPStatus.FOUND and "Max-Age=0" in out["Set-Cookie"]
        status, _, _ = client.request("/admin/", headers={"Cookie": cookie})
        assert status == HTTPStatus.UNAUTHORIZED


class TestAdminTestModeBanner:
    def test_banner_visible_when_test_mode_on(self, tmp_path: Path) -> None:
        state = build_state(tmp_path, test_mode=True)
        try:
            with running(state) as port:
                client = Client(port)
                _, headers = client.login(ADMIN_TOKEN)
                cookie = headers["Set-Cookie"].split(";")[0]
                _, body, _ = client.request("/admin/", headers={"Cookie": cookie})
        finally:
            state.database.close()
        assert "Включён тестовый режим Robokassa" in body


class TestHealthz:
    def test_public_answer_is_minimal(self, client: Client) -> None:
        """Наружу нельзя показывать состояние платёжного контура и время рестарта."""
        status, body, _ = client.request("/healthz")
        assert status == HTTPStatus.OK
        assert json.loads(body) == {"status": "ok"}

    def test_details_visible_with_admin_token(self, client: Client) -> None:
        status, body, _ = client.request("/healthz", headers={"X-Admin-Token": ADMIN_TOKEN})
        payload = json.loads(body)
        assert status == HTTPStatus.OK
        assert payload["status"] == "ok" and payload["db"] == "ok"
        assert payload["payments"] in {"on", "off"}
        assert payload["robokassa_test_mode"] is False
        assert "uptime_s" in payload and "maintenance" in payload

    def test_details_hidden_from_wrong_token(self, client: Client) -> None:
        _, body, _ = client.request("/healthz", headers={"X-Admin-Token": "wrong"})
        assert json.loads(body) == {"status": "ok"}

    def test_test_mode_visible_to_admin(self, tmp_path: Path) -> None:
        state = build_state(tmp_path, test_mode=True)
        try:
            with running(state) as port:
                _, body, _ = Client(port).request(
                    "/healthz", headers={"X-Admin-Token": ADMIN_TOKEN}
                )
        finally:
            state.database.close()
        assert json.loads(body)["robokassa_test_mode"] is True


class TestUnsupportedMethods:
    @pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
    def test_answer_is_our_405_page(self, client: Client, method: str) -> None:
        """http.server отдавал бы английскую 501 — на нашем сайте так быть не должно."""
        status, body, headers = client.request("/", method=method)
        assert status == HTTPStatus.METHOD_NOT_ALLOWED
        assert headers["Allow"] == "GET, HEAD, POST"
        assert "Метод не поддерживается" in body

    def test_result_url_ignores_exotic_methods(self, client: Client) -> None:
        """PUT на колбэк не должен уходить в разбор формы."""
        status, _, headers = client.request("/robokassa/result", method="PUT")
        assert status == HTTPStatus.METHOD_NOT_ALLOWED
        assert headers["Allow"] == "GET, HEAD, POST"


class TestSocketTimeout:
    def test_handler_declares_timeouts(self) -> None:
        assert isinstance(AppHandler.timeout, (int, float)) and AppHandler.timeout > 0
        assert 0 < AppHandler.request_timeout <= AppHandler.timeout
        # RU: Простаивающее соединение должен закрывать nginx (keepalive_timeout 60s),
        # иначе гонка на закрытии даёт 502 — в том числе на /robokassa/result.
        assert AppHandler.timeout > 60

    def test_slow_client_is_disconnected(self, tmp_path: Path) -> None:
        """Недосланное тело держало поток вечно: поток на соединение — и памяти нет."""
        state = build_state(tmp_path)
        try:
            with (
                running(state, handler_timeout=0.5) as port,
                socket.create_connection(("127.0.0.1", port), timeout=5) as sock,
            ):
                    sock.sendall(
                        b"POST /api/track HTTP/1.1\r\nHost: t\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 4096\r\n\r\n{"
                    )
                    sock.settimeout(5)
                    try:
                        while sock.recv(4096):
                            pass
                    except TimeoutError:
                        pytest.fail("соединение не закрылось — поток завис бы в rfile.read()")
        finally:
            state.database.close()
