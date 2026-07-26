"""HTTP-слой.

Один процесс ``ThreadingHTTPServer`` за nginx. Никаких фреймворков: маршрутов
десятки, а каждая зависимость на сервере, где живут пять таких проектов, стоит
памяти.

Отличия от учебного примера ``http.server``, без которых в проде будет больно:

* ``protocol_version = "HTTP/1.1"`` — иначе keep-alive выключен и nginx
  переоткрывает соединение на каждый запрос. Следствие: ``Content-Length``
  обязателен в КАЖДОМ ответе, иначе клиент зависнет.
* Слушаем строго ``127.0.0.1``. Наружу смотрит только nginx, он же держит TLS,
  сжатие, rate limit и заголовки безопасности.
* Тело запроса ограничено по размеру — иначе один POST способен съесть память.
* Логи идут в stdout построчным JSON: systemd заберёт их в journald.

Обработчики намеренно тонкие: они разбирают запрос и зовут функции-строители из
``handlers.py``, которые ничего не знают про HTTP. Благодаря этому весь тест
приложения работает без сокетов и без базы.

Go migration notes:
- Соответствует cmd/server + internal/http; таблицу маршрутов сохранить.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import secrets
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import handlers
from .config import RuntimeConfig, SiteConfig, load_runtime_config, load_site_config
from .db import Database
from .documents.registry import documents_payload
from .documents.wizard import wizard_payload
from .email_sender import send_order_email
from .logging_utils import log_event
from .products import DEFAULT_PRODUCT_CODE, get_product
from .repositories.metrics_repository import MetricsRepository
from .repositories.orders_repository import OrdersRepository
from .robokassa import load_robokassa_config, verify_success_callback
from .web.layout import render_error, render_maintenance, render_page
from .web.pages import PAGES_BY_PATH, sitemap_entries
from .web.pages.base import PageContext

# RU: 64 КБ с запасом хватает на любую нашу форму; больше принимать незачем.
MAX_BODY_BYTES = 64 * 1024

STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
}

STARTED_AT = time.time()

# RU: Методы, которые сайт действительно обслуживает. Значение уезжает в заголовок
# ``Allow`` ответа 405 — по RFC 7231 он там обязателен.
ALLOWED_METHODS = "GET, HEAD, POST"

ADMIN_COOKIE_NAME = "dokumatika_admin"
ADMIN_SESSION_TTL_S = 12 * 3600
# RU: Владелец один; потолок нужен только чтобы неудачные входы не копили мусор.
MAX_ADMIN_SESSIONS = 16


class AdminSessions:
    """Сессии админки в памяти процесса.

    В куку кладётся одноразовый идентификатор, а не сам ``ADMIN_TOKEN``: кука
    живёт в браузере и в его хранилище, токен — в ``.env``, и утечка первой не
    должна раскрывать второй. Хранилище в памяти выбрано осознанно: перезапуск
    сервиса разлогинивает, но для одной админки это дешевле таблицы в базе.
    """

    def __init__(self, ttl_s: int = ADMIN_SESSION_TTL_S) -> None:
        self._ttl = int(ttl_s)
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= MAX_ADMIN_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)), None)
            self._sessions[session_id] = now + self._ttl
        return session_id

    def is_valid(self, session_id: str) -> bool:
        candidate = str(session_id or "")
        if not candidate:
            return False
        with self._lock:
            self._prune(time.time())
            known = list(self._sessions)
        # RU: Идентификатор сессии — такой же секрет, как токен: сравниваем за
        # постоянное время и в байтах (compare_digest на не-ASCII str кидает TypeError).
        needle = candidate.encode("utf-8")
        return any(secrets.compare_digest(item.encode("utf-8"), needle) for item in known)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(str(session_id or ""), None)

    def _prune(self, now: float) -> None:
        for key in [key for key, expires_at in self._sessions.items() if expires_at <= now]:
            del self._sessions[key]


class AppState:
    """Собранное приложение: конфиги, база, репозитории.

    Живёт в одном экземпляре и передаётся обработчикам. Тесты создают свой
    экземпляр с временной базой — глобального состояния в модуле нет.
    """

    def __init__(
        self,
        *,
        site: SiteConfig,
        runtime: RuntimeConfig,
        database: Database,
    ) -> None:
        self.site = site
        self.runtime = runtime
        self.database = database
        self.orders = OrdersRepository(database)
        self.metrics = MetricsRepository(database)
        self.admin_sessions = AdminSessions()
        self.robokassa = load_robokassa_config()
        self.product = get_product(DEFAULT_PRODUCT_CODE)
        if self.product is None:  # pragma: no cover - защита от опечатки в каталоге
            raise RuntimeError("default product is missing from catalog")

    def ensure_schema(self) -> None:
        self.orders.ensure_schema()
        self.metrics.ensure_schema()

    @property
    def payments_enabled(self) -> bool:
        """Оплата работает, только если она включена И настроена."""
        return bool(self.runtime.payments_enabled and self.robokassa is not None)

    def page_context(self) -> PageContext:
        return PageContext(
            site=self.site,
            runtime=self.runtime,
            product=self.product,
            payments_enabled=self.payments_enabled,
        )


def build_state(*, site: SiteConfig | None = None, runtime: RuntimeConfig | None = None) -> AppState:
    resolved_runtime = runtime or load_runtime_config()
    state = AppState(
        site=site or load_site_config(),
        runtime=resolved_runtime,
        database=Database(resolved_runtime.database_path),
    )
    state.ensure_schema()
    return state


class AppHandler(BaseHTTPRequestHandler):
    # RU: keep-alive с nginx. Требует Content-Length в каждом ответе — см. _send.
    protocol_version = "HTTP/1.1"
    server_version = "dokumatika"
    sys_version = ""

    # RU: Без таймаута поток навсегда виснет в rfile.read() на недосланном теле, а
    # поток здесь создаётся на КАЖДОЕ соединение. StreamRequestHandler.setup() сам
    # применит это значение к сокету.
    #
    # Два разных срока не от любви к настройкам. `timeout` — это ещё и ожидание
    # СЛЕДУЮЩЕГО запроса на keep-alive-соединении, и он обязан быть больше
    # `keepalive_timeout 60s` у апстрима в nginx: если простаивающее соединение
    # первым закроет приложение, nginx может отдать 502 — в том числе на
    # /robokassa/result. А на чтение уже начатого запроса столько не нужно.
    timeout = 75
    request_timeout = 20

    state: AppState  # проставляется фабрикой ниже

    # --------------------------------------------------------------- ответы

    def _send(
        self,
        status: HTTPStatus | int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        max_age: int | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if max_age is not None:
            self.send_header(
                "Cache-Control",
                f"public, max-age={max_age}, immutable" if max_age > 0 else "no-store",
            )
        for name, value in extra_headers:
            # RU: Заголовки http.server не проверяет на CRLF — проверяем сами,
            # иначе значение с переводом строки расщепит ответ.
            if "\r" in value or "\n" in value:
                continue
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, status: HTTPStatus | int, html: str, **kwargs: Any) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", **kwargs)

    def _send_json(self, status: HTTPStatus | int, payload: Any, **kwargs: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", **kwargs)

    def _send_text(self, status: HTTPStatus | int, text: str, **kwargs: Any) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8", **kwargs)

    def _send_error_page(
        self,
        status: HTTPStatus,
        title: str,
        message: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        html = render_error(
            self.state.site, self.state.runtime, code=int(status), title=title, message=message
        )
        self._send_html(status, html, max_age=0, extra_headers=extra_headers)

    # ---------------------------------------------------------------- ввод

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _read_body(self) -> bytes:
        self._body_consumed = True
        length = self._content_length()
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            # RU: Слишком большое тело не читаем, но тогда обязаны закрыть
            # соединение: непрочитанные байты остались бы в сокете и при
            # keep-alive были бы разобраны как следующий запрос.
            self.close_connection = True
            return b""
        return self.rfile.read(length)

    def _drain_body(self) -> None:
        """Дочитать тело, которого не коснулся обработчик.

        При ``HTTP/1.1`` соединение переиспользуется, и остаток тела съехал бы в
        начало следующего запроса — классическая десинхронизация. Дешевле
        вычитать, а на слишком большом теле просто закрыть соединение.
        """
        if getattr(self, "_body_consumed", False):
            return
        length = self._content_length()
        if length <= 0:
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def _read_form(self) -> dict[str, str]:
        """Разобрать form-urlencoded тело в плоский словарь."""
        raw = self._read_body().decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _client_ip(self) -> str:
        """Реальный IP из заголовка nginx, иначе адрес сокета.

        Доверять ``X-Forwarded-For`` можно только потому, что приложение слушает
        loopback и снаружи недостижимо — единственный источник заголовка nginx.
        """
        forwarded = str(self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For") or "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
        return str(self.client_address[0]) if self.client_address else ""

    # -------------------------------------------------------------- логи

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - сигнатура базового класса
        """Заглушить стандартный текстовый лог — пишем структурно и сами."""
        return

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        log_event("http_error", detail=(format % args) if args else format)

    # ------------------------------------------------------------ маршруты

    def do_GET(self) -> None:  # noqa: N802 - имя задано базовым классом
        self._route("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    # RU: Без этих методов http.server отдаёт свою английскую страницу 501, а наша
    # ветка «Метод не поддерживается» в _route была недостижима.
    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._route("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._route("OPTIONS")

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        state = self.state
        # RU: Экземпляр обработчика переиспользуется на keep-alive-соединении,
        # поэтому пометку «тело прочитано» сбрасываем на каждый запрос.
        self._body_consumed = False
        # RU: Заголовки уже прочитаны — остаток запроса ждём по короткому сроку,
        # а долгое ожидание возвращаем перед следующим запросом в соединении.
        self._set_socket_timeout(self.request_timeout)

        try:
            # RU: Колбэк платёжной системы работает даже в maintenance — иначе
            # деньги спишутся, а заказ останется неоплаченным.
            if path == "/robokassa/result" and method in {"GET", "POST"}:
                self._handle_robokassa_result(method, query)
                return

            if path == "/healthz" and method == "GET":
                self._handle_healthz()
                return

            if state.runtime.is_maintenance():
                self._send_html(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    render_maintenance(state.site),
                    extra_headers=(("Retry-After", "3600"),),
                    max_age=0,
                )
                return

            if method == "GET" and self._handle_static(path, query):
                return

            if method == "GET":
                self._handle_get(path, query)
                return
            if method == "POST":
                self._handle_post(path, query)
                return

            self._send_error_page(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Метод не поддерживается",
                "Страницу можно открыть или отправить форму — других способов у сайта нет.",
                extra_headers=(("Allow", ALLOWED_METHODS),),
            )
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - клиент отвалился
            self.close_connection = True
            return
        except TimeoutError:
            # RU: Клиент не дослал тело за отведённое время. Отвечать некому —
            # тихо закрываем соединение и освобождаем поток.
            self.close_connection = True
            log_event("request_timeout", path=path, method=method)
            return
        except Exception as error:  # pragma: no cover - последний рубеж
            log_event("unhandled_error", path=path, method=method, error=repr(error))
            self._send_error_page(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Что-то пошло не так",
                "Мы уже знаем о проблеме. Попробуйте обновить страницу через минуту.",
            )
        finally:
            self._drain_body()
            self._set_socket_timeout(self.timeout)

    def _set_socket_timeout(self, value: float | None) -> None:
        # RU: сокет мог уже закрыться — тогда таймаут ставить не на что.
        with contextlib.suppress(OSError):
            self.connection.settimeout(value)

    # ----------------------------------------------------------- статика

    def _handle_static(self, path: str, query: dict[str, str]) -> bool:
        state = self.state
        root = state.runtime.static_root

        if path == "/robots.txt":
            self._send_text(HTTPStatus.OK, handlers.build_robots_txt(state.site), max_age=3600)
            return True

        if path == "/sitemap.xml":
            body = handlers.build_sitemap(state.site, sitemap_entries())
            self._send(HTTPStatus.OK, body.encode("utf-8"), "application/xml; charset=utf-8", max_age=3600)
            return True

        # RU: Файл-подтверждение владения ключом IndexNow.
        key = state.site.indexnow_key
        if key and path == f"/{key}.txt":
            self._send_text(HTTPStatus.OK, key, max_age=3600)
            return True

        candidate = self._resolve_static_path(root, path)
        if candidate is None:
            return False

        try:
            payload = candidate.read_bytes()
        except OSError:
            return False

        suffix = candidate.suffix.lower()
        content_type = STATIC_CONTENT_TYPES.get(suffix) or mimetypes.guess_type(candidate.name)[0]
        # RU: Версионированный URL можно кэшировать вечно; без ?v= — сутки.
        max_age = 31536000 if query.get("v") else 86400
        self._send(HTTPStatus.OK, payload, content_type or "application/octet-stream", max_age=max_age)
        return True

    @staticmethod
    def _resolve_static_path(root: Path, path: str) -> Path | None:
        """Сопоставить URL файлу внутри static, не выпуская за его пределы."""
        if path in {"", "/"} or path.endswith("/"):
            return None
        relative = path.lstrip("/")
        if not relative or ".." in relative:
            return None
        try:
            candidate = (root / relative).resolve()
            root_resolved = root.resolve()
        except OSError:
            return None
        if not candidate.is_file():
            return None
        # RU: Ключевая проверка: файл обязан лежать внутри static-корня.
        if root_resolved not in candidate.parents:
            return None
        return candidate

    # --------------------------------------------------------------- GET

    def _handle_get(self, path: str, query: dict[str, str]) -> None:
        state = self.state

        if path == "/api/wizard.json":
            # RU: Публично — только бесплатный шаблон. Платный комплект и есть
            # то, за что платят: отдавать его открытым API означало бы продавать
            # то, что рядом лежит бесплатно.
            payload = {"wizard": wizard_payload(), "documents": documents_payload(include_paid=False)}
            self._send_json(HTTPStatus.OK, payload, max_age=3600)
            return

        if path == "/api/package.json":
            self._handle_package_api(str(query.get("token") or ""))
            return

        if path.startswith("/pay/"):
            self._handle_pay_redirect(path.removeprefix("/pay/").strip("/"))
            return

        if path.startswith("/zakaz/"):
            self._handle_order_page(path.removeprefix("/zakaz/").strip("/"))
            return

        if path == "/oplata/uspeh/":
            self._handle_payment_return(query, success=True)
            return

        if path == "/oplata/otmena/":
            self._handle_payment_return(query, success=False)
            return

        if path == "/admin/":
            self._handle_admin(query)
            return

        page = PAGES_BY_PATH.get(path)
        if page is None:
            # RU: Мягкая нормализация: /foo -> /foo/ вместо 404.
            if not path.endswith("/") and PAGES_BY_PATH.get(path + "/") is not None:
                self._redirect(path + "/", permanent=True)
                return
            self._send_error_page(
                HTTPStatus.NOT_FOUND,
                "Страница не найдена",
                "Возможно, ссылка устарела. Начните с главной — генератор политики там.",
            )
            return

        meta, body = page.build(state.page_context())
        html = render_page(
            site=state.site,
            runtime=state.runtime,
            meta=meta,
            body=body,
            extra_scripts=page.scripts,
        )
        self._send_html(HTTPStatus.OK, html, max_age=0)

    def _redirect(self, location: str, *, permanent: bool = False) -> None:
        status = HTTPStatus.MOVED_PERMANENTLY if permanent else HTTPStatus.FOUND
        self._send(
            status,
            b"",
            "text/plain; charset=utf-8",
            extra_headers=(("Location", location),),
            max_age=0,
        )

    # -------------------------------------------------------------- POST

    def _handle_post(self, path: str, query: dict[str, str]) -> None:
        length = self._content_length()
        if length > MAX_BODY_BYTES or length < 0:
            # RU: Честный 413 вместо тихо опустевшего тела: иначе клиент видит
            # «проверьте email» там, где на самом деле не пролез запрос.
            self.close_connection = True
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "payload_too_large", "message": "Слишком большой запрос"},
                max_age=0,
            )
            return
        if path == "/api/order":
            self._handle_create_order()
            return
        if path == "/api/track":
            self._handle_track()
            return
        if path == "/admin/":
            self._handle_admin_form()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"}, max_age=0)

    def _handle_track(self) -> None:
        payload = self._read_json()
        tracked = self.state.metrics.track(
            str(payload.get("event") or ""), str(payload.get("label") or "")
        )
        self._send_json(HTTPStatus.OK if tracked else HTTPStatus.BAD_REQUEST, {"ok": tracked}, max_age=0)

    def _handle_create_order(self) -> None:
        state = self.state
        payload = self._read_json()
        result = handlers.build_order_creation(
            payload=payload,
            product=state.product,
            robokassa=state.robokassa,
            payments_enabled=state.payments_enabled,
            orders=state.orders,
            site=state.site,
            client_ip=self._client_ip(),
            user_agent=str(self.headers.get("User-Agent") or "")[:255],
        )
        body, status = result
        if status == HTTPStatus.OK:
            state.metrics.track("checkout_created", state.product.code)
        self._send_json(status, body, max_age=0)

    # ---------------------------------------------------------- оплата

    def _handle_pay_redirect(self, access_token: str) -> None:
        """Страница-переходник: автоотправка POST-формы на Robokassa."""
        state = self.state
        order = state.orders.get_by_access_token(access_token)
        if order is None:
            self._send_error_page(
                HTTPStatus.NOT_FOUND,
                "Заказ не найден",
                "Проверьте ссылку из письма или создайте заказ заново.",
            )
            return
        if order.is_paid:
            self._redirect(f"/zakaz/{order.access_token}/")
            return
        if order.is_terminal:
            # RU: Заказ закрыт (отказ банка или отмена, подтверждённая сверкой).
            # Отдавать по нему подписанную форму оплаты нельзя: тот же InvId
            # Robokassa второй раз не примет — вернёт ошибку 40.
            self._send_error_page(
                HTTPStatus.GONE,
                "Заказ закрыт",
                "Эта попытка оплаты больше не действует. Оформите заказ заново — это займёт минуту.",
            )
            return
        if not state.payments_enabled or state.robokassa is None:
            self._send_error_page(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Оплата временно недоступна",
                "Приём платежей приостановлен. Попробуйте позже.",
            )
            return

        meta, body = handlers.build_checkout_page(
            order=order, product=state.product, robokassa=state.robokassa, site=state.site
        )
        state.orders.mark_pending(order.order_id)
        html = render_page(
            site=state.site, runtime=state.runtime, meta=meta, body=body, extra_scripts=("pay.js",)
        )
        self._send_html(HTTPStatus.OK, html, max_age=0)

    def _handle_package_api(self, access_token: str) -> None:
        """Шаблоны платного комплекта — только по токену оплаченного заказа."""
        state = self.state
        order = state.orders.get_by_access_token(access_token)
        if order is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "order_not_found"}, max_age=0)
            return
        if not order.is_paid:
            self._send_json(HTTPStatus.PAYMENT_REQUIRED, {"error": "not_paid"}, max_age=0)
            return
        payload = {"wizard": wizard_payload(), "documents": documents_payload(include_paid=True)}
        # RU: no-store: ответ привязан к конкретному заказу и не должен осесть
        # в общем кэше или у прокси.
        self._send_json(HTTPStatus.OK, payload, max_age=0)

    def _handle_order_page(self, access_token: str) -> None:
        state = self.state
        order = state.orders.get_by_access_token(access_token)
        if order is None:
            self._send_error_page(
                HTTPStatus.NOT_FOUND,
                "Заказ не найден",
                "Проверьте ссылку — возможно, она скопирована не целиком.",
            )
            return
        meta, body = handlers.build_order_page(order=order, product=state.product, site=state.site)
        scripts = ("wizard.js", "docgen.js", "package.js") if order.is_paid else ()
        html = render_page(
            site=state.site, runtime=state.runtime, meta=meta, body=body, extra_scripts=scripts
        )
        self._send_html(HTTPStatus.OK, html, max_age=0)

    def _handle_payment_return(self, query: dict[str, str], *, success: bool) -> None:
        """Возврат пользователя с платёжной страницы.

        Страница показывает статус и НИЧЕГО не меняет — ни в плюс, ни в минус.
        Это принципиально: адрес возврата открывается браузером покупателя, его
        может открыть кто угодно с любыми параметрами.

        Раньше здесь стояла отмена заказа по FailURL, и она создавала дыру:
        покупатель платит, ResultURL задерживается, покупатель жмёт «вернуться в
        магазин» — заказ отменён, пришедший следом платёж уже не принимается.
        Брошенные заказы закрывает ``scripts/reconcile_payments.py``, который
        спрашивает статус у самой Robokassa.

        Заказ раскрывается только при верной подписи (SuccessURL подписывается
        Паролем #1). Без неё ``InvId`` — просто число из адресной строки, и по
        нему нельзя было бы отдавать ссылку на чужие документы.
        """
        state = self.state
        order = None
        shp_order = str(query.get("Shp_order_id") or "").strip()
        if shp_order and state.robokassa is not None:
            candidate = state.orders.get_by_id(shp_order)
            if candidate is not None:
                verification = verify_success_callback(
                    query, config=state.robokassa, is_test=candidate.is_test
                )
                if verification.ok:
                    order = candidate
                else:
                    log_event(
                        "payment_return_unsigned",
                        order_id=candidate.order_id,
                        reason=verification.reason,
                        success=success,
                    )

        if order is not None and success and order.is_paid:
            self._redirect(f"/zakaz/{order.access_token}/")
            return

        meta, body = handlers.build_payment_return_page(
            order=order, product=state.product, site=state.site, success=success
        )
        html = render_page(site=state.site, runtime=state.runtime, meta=meta, body=body)
        self._send_html(HTTPStatus.OK, html, max_age=0)

    def _handle_robokassa_result(self, method: str, query: dict[str, str]) -> None:
        """Единственная точка, где заказ становится оплаченным."""
        state = self.state
        payload = dict(query) if method == "GET" else {**query, **self._read_form()}

        text, status, order = handlers.apply_robokassa_result(
            payload=payload,
            orders=state.orders,
            product=state.product,
            robokassa=state.robokassa,
        )
        if order is not None and status == HTTPStatus.OK and order.is_paid and not order.delivered_at:
            state.metrics.track("order_paid", order.product_code)
            self._deliver_order(order)
        self._send_text(status, text, max_age=0)

    def _deliver_order(self, order: Any) -> None:
        """Отправить письмо со ссылкой на документы — в фоне.

        Синхронная отправка была бы прямой угрозой платежам: SMTP-соединение
        может висеть до таймаута, а nginx обрывает запрос через 30 секунд.
        Robokassa не получила бы ``OK<InvId>`` и начала бы слать колбэк заново.
        Письмо здесь — приятное дополнение: ссылка на заказ уже показана
        покупателю на экране, так что его задержка или потеря не критичны.
        """
        state = self.state

        def deliver() -> None:
            try:
                sent = send_order_email(
                    smtp=state.runtime.smtp,
                    site=state.site,
                    product=state.product,
                    order=order,
                )
            except Exception as error:  # pragma: no cover - сеть недоступна в тестах
                log_event("order_email_failed", order_id=order.order_id, error=repr(error))
                return
            if sent:
                state.orders.mark_delivered(order.order_id)
                log_event("order_email_sent", order_id=order.order_id, email=order.email)

        threading.Thread(target=deliver, name=f"email-{order.order_id}", daemon=True).start()

    # --------------------------------------------------------- служебное

    def _handle_healthz(self) -> None:
        """Наружу — только «жив или нет».

        Состояние платёжного контура, время с рестарта и размер WAL — это
        разведданные: по ним видно окно, когда ResultURL мог не дойти, и включён
        ли приём оплаты. Подробности отдаём только по админ-токену.
        """
        state = self.state
        detailed = self._is_admin()
        try:
            health = state.database.healthcheck()
        except Exception as error:
            payload: dict[str, Any] = {"status": "error"}
            if detailed:
                payload["db"] = repr(error)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, payload, max_age=0)
            return
        if not detailed:
            self._send_json(HTTPStatus.OK, {"status": "ok"}, max_age=0)
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "uptime_s": int(time.time() - STARTED_AT),
                "payments": "on" if state.payments_enabled else "off",
                "maintenance": state.runtime.is_maintenance(),
                "robokassa_test_mode": bool(state.robokassa is not None and state.robokassa.test_mode),
                **health,
            },
            max_age=0,
        )

    # ----------------------------------------------------------- админка

    def _cookie(self, name: str) -> str:
        raw = str(self.headers.get("Cookie") or "")
        if not raw:
            return ""
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return ""
        morsel = jar.get(name)
        return str(morsel.value) if morsel is not None else ""

    def _admin_token_matches(self, provided: str) -> bool:
        token = self.state.runtime.admin_token
        if not token or not provided:
            return False
        # RU: Сравниваем байты: compare_digest на str с не-ASCII кидает TypeError,
        # и вместо 403 получалась бы 500 — она же подсказка, что токен настроен.
        return secrets.compare_digest(provided.encode("utf-8"), token.encode("utf-8"))

    def _is_admin(self) -> bool:
        """Авторизован ли запрос: кука сессии или заголовок ``X-Admin-Token``.

        Токен из query-строки НЕ принимается сознательно: ``?token=...`` целиком
        ложится в access-лог nginx, в историю браузера, в автодополнение и в
        Referer — а по нему видны e-mail всех покупателей. Сайт не выкачен, так
        что обратной совместимости беречь не для кого.
        """
        if not self.state.runtime.admin_token:
            return False
        if self._admin_token_matches(str(self.headers.get("X-Admin-Token") or "")):
            return True
        return self.state.admin_sessions.is_valid(self._cookie(ADMIN_COOKIE_NAME))

    @staticmethod
    def _admin_cookie(value: str, *, max_age: int) -> str:
        """Кука сессии: недоступна JS, только по HTTPS, не уходит с чужих сайтов."""
        return (
            f"{ADMIN_COOKIE_NAME}={value}; Path=/admin/; Max-Age={int(max_age)}; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    def _send_admin_login(
        self, status: HTTPStatus, *, error: str = "", query_token_seen: bool = False
    ) -> None:
        state = self.state
        meta, body = handlers.build_admin_login_page(
            site=state.site, error=error, query_token_seen=query_token_seen
        )
        html = render_page(site=state.site, runtime=state.runtime, meta=meta, body=body)
        headers: tuple[tuple[str, str], ...] = ()
        if status == HTTPStatus.UNAUTHORIZED:
            headers = (("WWW-Authenticate", 'Token realm="dokumatika-admin"'),)
        self._send_html(status, html, max_age=0, extra_headers=headers)

    def _handle_admin_form(self) -> None:
        """POST /admin/: вход по токену и выход."""
        state = self.state
        if not state.runtime.admin_token:
            self._send_error_page(
                HTTPStatus.NOT_FOUND, "Страница не найдена", "Админка отключена в настройках."
            )
            return
        form = self._read_form()
        if str(form.get("action") or "") == "logout":
            state.admin_sessions.drop(self._cookie(ADMIN_COOKIE_NAME))
            self._send(
                HTTPStatus.FOUND,
                b"",
                "text/plain; charset=utf-8",
                extra_headers=(("Location", "/admin/"), ("Set-Cookie", self._admin_cookie("", max_age=0))),
                max_age=0,
            )
            return
        if not self._admin_token_matches(str(form.get("token") or "")):
            log_event("admin_login_failed", ip=self._client_ip())
            self._send_admin_login(HTTPStatus.FORBIDDEN, error="Неверный токен.")
            return
        session_id = state.admin_sessions.create()
        log_event("admin_login_ok", ip=self._client_ip())
        # RU: После проверки — редирект на чистый /admin/ без параметров, дальше
        # ходим по куке.
        self._send(
            HTTPStatus.FOUND,
            b"",
            "text/plain; charset=utf-8",
            extra_headers=(
                ("Location", "/admin/"),
                ("Set-Cookie", self._admin_cookie(session_id, max_age=ADMIN_SESSION_TTL_S)),
            ),
            max_age=0,
        )

    def _handle_admin(self, query: dict[str, str]) -> None:
        """Минимальная админка: сводка заказов и воронки. Вход — формой, не ссылкой."""
        state = self.state
        if not state.runtime.admin_token:
            self._send_error_page(
                HTTPStatus.NOT_FOUND, "Страница не найдена", "Админка отключена в настройках."
            )
            return
        if not self._is_admin():
            self._send_admin_login(
                HTTPStatus.UNAUTHORIZED, query_token_seen=bool(query.get("token"))
            )
            return

        meta, body = handlers.build_admin_page(
            site=state.site,
            orders=state.orders.recent(50),
            stats=state.orders.stats(),
            funnel=state.metrics.totals(),
            payments_enabled=state.payments_enabled,
            robokassa_configured=state.robokassa is not None,
            test_mode=bool(state.robokassa.test_mode) if state.robokassa else False,
        )
        html = render_page(site=state.site, runtime=state.runtime, meta=meta, body=body)
        self._send_html(HTTPStatus.OK, html, max_age=0)


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer уже подмешивает ThreadingMixIn — второй раз не нужно."""

    daemon_threads = True
    # RU: Позволяет перезапускать сервис без ожидания TIME_WAIT.
    allow_reuse_address = True


def make_handler(state: AppState) -> type[AppHandler]:
    return type("BoundAppHandler", (AppHandler,), {"state": state})


def serve(state: AppState | None = None) -> None:
    app_state = state or build_state()
    handler_cls = make_handler(app_state)
    server = Server((app_state.runtime.host, app_state.runtime.port), handler_cls)

    def shutdown(signum: int, _frame: Any) -> None:
        log_event("server_stopping", signal=signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    test_mode = bool(app_state.robokassa is not None and app_state.robokassa.test_mode)
    log_event(
        "server_started",
        host=app_state.runtime.host,
        port=app_state.runtime.port,
        payments="on" if app_state.payments_enabled else "off",
        robokassa_test_mode=test_mode,
        database=str(app_state.runtime.database_path),
        pid=os.getpid(),
    )
    if test_mode:
        # RU: Отдельное событие, чтобы мониторинг мог алертить именно на него:
        # забытый ROBOKASSA_TEST_MODE=1 раздаёт комплект бесплатно.
        log_event(
            "robokassa_test_mode_enabled",
            severity="error",
            hint="деньги не списываются, комплект выдаётся бесплатно",
        )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app_state.database.close()
        log_event("server_stopped")


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
