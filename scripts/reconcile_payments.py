"""Сверка зависших заказов с Robokassa через OpStateExt.

Зачем это нужно. Заказ становится оплаченным ровно в одном месте — в обработчике
ResultURL. Robokassa повторяет уведомление, пока не получит ``OK<InvId>``, но
повторы конечны: если сервер лежал полчаса, у nginx отвалился сертификат или
IP-фильтр на ``/robokassa/result`` устарел, уведомление может не дойти вовсе.
Тогда деньги списаны, а заказ висит в ``pending``, и покупатель не получил
документы. Этот скрипт закрывает дыру: он сам спрашивает Robokassa о статусе
каждой зависшей операции.

Направление запроса важно: не «нам что-то прислали, поверим подписи», а «мы сами
сходили к платёжному сервису по HTTPS с подписью на Пароле #2». Подделать такой
ответ можно, только скомпрометировав TLS до auth.robokassa.ru.

ТЕСТОВЫЙ РЕЖИМ. OpStateExt тестовые операции не показывает — при
``ROBOKASSA_TEST_MODE=1`` скрипт честно откажется работать, а не будет молча
считать все тестовые заказы неоплаченными.

Запуск (раз в 10-15 минут по крону или руками):

    python3 scripts/reconcile_payments.py --env-file /etc/dokumatika/.env
    python3 scripts/reconcile_payments.py --env-file /etc/dokumatika/.env --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.config import RuntimeConfig, SiteConfig, load_runtime_config, load_site_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.email_sender import send_order_email  # noqa: E402
from app.products import DEFAULT_PRODUCT_CODE, get_product  # noqa: E402
from app.repositories.metrics_repository import MetricsRepository  # noqa: E402
from app.repositories.orders_repository import (  # noqa: E402
    STATUS_CREATED,
    STATUS_PENDING,
    Order,
    OrdersRepository,
)
from app.robokassa import (  # noqa: E402
    OPSTATE_CODES,
    OPSTATE_SUCCESS_CODE,
    RobokassaConfig,
    amount_matches_minor,
    build_opstate_request,
    load_robokassa_config,
)

USER_AGENT = "dokumatika-reconcile/1.0"

# RU: Статусы, из которых заказ ещё может стать оплаченным. Терминальные
# (paid/failed/canceled) не трогаем — это защита от «разоплаты» задним числом.
OPEN_STATUSES = frozenset({STATUS_CREATED, STATUS_PENDING})

# RU: Коды State, при которых деньги точно не придут. Используются только с
# флагом --cancel-failed: молча закрывать чужие заказы скрипт не должен.
DEAD_STATE_CODES = frozenset({10, 60})


def load_env_file(path: Path) -> int:
    """Прочитать ``KEY=VALUE`` из .env в окружение процесса."""
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1
    return loaded


# --------------------------------------------------------------- разбор XML


def local_name(tag: str) -> str:
    """Ответ приходит в пространстве имён merchant.roboxchange.com — режем его."""
    return tag.rsplit("}", 1)[-1]


def find_child(node: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if node is None:
        return None
    for element in list(node):
        if local_name(element.tag) == name:
            return element
    return None


def child_text(node: ElementTree.Element | None, *path: str) -> str:
    current = node
    for name in path:
        current = find_child(current, name)
    return (current.text or "").strip() if current is not None else ""


@dataclass(frozen=True)
class OpState:
    """Разобранный ответ OpStateExt."""

    result_code: int
    result_description: str
    state_code: int
    state_date: str
    out_sum: str

    @property
    def query_ok(self) -> bool:
        """Сервис принял запрос. Это ещё не значит, что операция оплачена."""
        return self.result_code == 0

    @property
    def is_paid(self) -> bool:
        return self.query_ok and self.state_code == OPSTATE_SUCCESS_CODE

    @property
    def state_text(self) -> str:
        return OPSTATE_CODES.get(self.state_code, f"код {self.state_code}")


def parse_opstate(raw: bytes) -> OpState:
    root = ElementTree.fromstring(raw)
    return OpState(
        result_code=_as_int(child_text(root, "Result", "Code"), default=-1),
        result_description=child_text(root, "Result", "Description"),
        state_code=_as_int(child_text(root, "State", "Code"), default=-1),
        state_date=child_text(root, "State", "StateDate"),
        # RU: OutSum — сумма, которую получает магазин, то есть ровно та, что мы
        # отправляли в форме. IncSum больше на комиссию покупателя и для сверки
        # с нашей записью не годится.
        out_sum=child_text(root, "Info", "OutSum"),
    )


def _as_int(value: str, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def request_opstate(url: str, params: dict[str, str], *, timeout: float) -> bytes:
    """POST, а не GET: подпись не должна оседать в логах и истории прокси."""
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read())


# ------------------------------------------------------------------ отбор


def parse_timestamp(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def select_stale(orders: list[Order], *, min_age: timedelta, max_age: timedelta) -> list[Order]:
    """Заказы, по которым имеет смысл спрашивать статус.

    Нижняя граница по возрасту нужна, чтобы не догонять нормальный платёж,
    уведомление о котором ещё в пути. Верхняя — чтобы не долбить сервис
    вопросами про брошенные корзины полугодовой давности.
    """
    now = datetime.now(timezone.utc)
    selected: list[Order] = []
    for order in orders:
        if order.status not in OPEN_STATUSES or not order.invoice_id:
            continue
        created = parse_timestamp(order.created_at)
        if created is None:
            continue
        age = now - created
        if min_age <= age <= max_age:
            selected.append(order)
    return selected


# ----------------------------------------------------------------- главное


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сверка зависших заказов с Robokassa")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env")
    parser.add_argument("--min-age-minutes", type=int, default=30, help="не трогать заказы моложе, минут")
    parser.add_argument("--max-age-days", type=int, default=30, help="не трогать заказы старше, дней")
    parser.add_argument("--scan", type=int, default=200, help="сколько последних заказов просмотреть")
    parser.add_argument("--order", default="", help="сверить один конкретный заказ по order_id")
    parser.add_argument("--timeout", type=float, default=20.0, help="таймаут запроса, секунд")
    parser.add_argument("--dry-run", action="store_true", help="только показать, ничего не менять")
    parser.add_argument("--no-email", action="store_true", help="не отправлять письмо покупателю")
    parser.add_argument(
        "--cancel-failed",
        action="store_true",
        help="закрывать заказы, по которым Robokassa сообщила об отмене или возврате",
    )
    args = parser.parse_args(argv)

    if args.env_file is not None:
        if not args.env_file.is_file():
            print(f"Файл окружения не найден: {args.env_file}")
            return 2
        load_env_file(args.env_file)

    robokassa = load_robokassa_config()
    if robokassa is None:
        print("Robokassa не настроена (нет ROBOKASSA_MERCHANT_LOGIN/PASSWORD1/PASSWORD2).")
        return 2
    if robokassa.test_mode:
        print("Включён тестовый режим: OpStateExt тестовые операции не показывает. Сверка невозможна.")
        return 2

    site = load_site_config()
    runtime = load_runtime_config()
    if not runtime.database_path.exists():
        print(f"База не найдена: {runtime.database_path}")
        return 2

    database = Database(runtime.database_path)
    orders_repo = OrdersRepository(database)
    metrics = MetricsRepository(database)

    try:
        if args.order:
            single = orders_repo.get_by_id(args.order.strip())
            candidates = [single] if single is not None else []
            if not candidates:
                print(f"Заказ не найден: {args.order}")
                return 2
        else:
            candidates = select_stale(
                orders_repo.recent(max(1, args.scan)),
                min_age=timedelta(minutes=max(0, args.min_age_minutes)),
                max_age=timedelta(days=max(1, args.max_age_days)),
            )

        if not candidates:
            print("Зависших заказов нет — сверять нечего.")
            return 0

        print(f"К сверке: {len(candidates)}")
        paid = 0
        canceled = 0
        errors = 0

        for index, order in enumerate(candidates):
            if index:
                # RU: Не частим: сервис статусов не рассчитан на поток запросов.
                time.sleep(0.5)
            outcome = reconcile_one(
                order,
                robokassa=robokassa,
                orders_repo=orders_repo,
                metrics=metrics,
                runtime=runtime,
                site=site,
                timeout=args.timeout,
                dry_run=args.dry_run,
                send_email=not args.no_email,
                cancel_failed=args.cancel_failed,
            )
            paid += 1 if outcome == "paid" else 0
            canceled += 1 if outcome == "canceled" else 0
            errors += 1 if outcome == "error" else 0

        print(f"\nИтог: оплачено {paid}, закрыто как неоплаченные {canceled}, ошибок {errors}")
        if args.dry_run:
            print("Это был --dry-run: в базе ничего не менялось.")
        return 1 if errors else 0
    finally:
        database.close()


def reconcile_one(
    order: Order,
    *,
    robokassa: RobokassaConfig,
    orders_repo: OrdersRepository,
    metrics: MetricsRepository,
    runtime: RuntimeConfig,
    site: SiteConfig,
    timeout: float,
    dry_run: bool,
    send_email: bool,
    cancel_failed: bool,
) -> str:
    """Сверить один заказ. Возвращает ``paid`` / ``canceled`` / ``open`` / ``error``."""
    prefix = f"{order.order_id} (InvId {order.invoice_id}, {order.status})"

    if order.is_test:
        print(f"[--] {prefix}: тестовый заказ, OpStateExt его не видит")
        return "open"

    url, params = build_opstate_request(robokassa, order.invoice_id)
    try:
        raw = request_opstate(url, params, timeout=timeout)
        state = parse_opstate(raw)
    except (urllib.error.URLError, OSError) as error:
        print(f"[!!] {prefix}: запрос не прошёл — {error}")
        return "error"
    except ElementTree.ParseError as error:
        print(f"[!!] {prefix}: ответ не разобрался — {error}")
        return "error"

    if not state.query_ok:
        # RU: Ненулевой Result/Code — это про сам запрос (подпись, неизвестная
        # операция), а не про оплату. Описание печатаем как есть, от Robokassa.
        print(f"[!!] {prefix}: сервис вернул код {state.result_code} «{state.result_description}»")
        return "error"

    if not state.is_paid:
        if state.state_code in DEAD_STATE_CODES and cancel_failed:
            if dry_run:
                print(f"[dry] {prefix}: закрыл бы как неоплаченный — {state.state_text}")
                return "canceled"
            orders_repo.mark_canceled(order.order_id)
            print(f"[ok] {prefix}: закрыт — {state.state_text}")
            return "canceled"
        print(f"[--] {prefix}: не оплачен — {state.state_text}")
        return "open"

    # RU: Сумма сверяется с нашей записью, а не принимается на веру: если она
    # разошлась, это повод разбираться руками, а не выдавать документы.
    if state.out_sum and not amount_matches_minor(state.out_sum, order.amount_minor):
        print(
            f"[!!] {prefix}: сумма не сходится — у Robokassa {state.out_sum}, "
            f"у нас {order.amount_minor / 100:.2f}. Разбирайтесь вручную."
        )
        return "error"

    if dry_run:
        print(f"[dry] {prefix}: отметил бы оплаченным (State {state.state_code}, {state.state_date})")
        return "paid"

    updated, applied = orders_repo.apply_paid_callback(
        # RU: Свой префикс event_id, чтобы сверка и запоздавший ResultURL не
        # оплатили заказ дважды и не отправили покупателю два письма.
        event_id=f"opstate:{order.invoice_id}",
        order_id=order.order_id,
        metadata_patch={
            "reconciled": True,
            "opstate_code": state.state_code,
            "opstate_date": state.state_date,
        },
    )
    if updated is None:
        print(f"[!!] {prefix}: заказ исчез из базы во время сверки")
        return "error"
    if not applied:
        print(f"[--] {prefix}: уже был обработан (сейчас {updated.status})")
        return "open"

    metrics.track("order_paid", updated.product_code)
    print(f"[ok] {prefix}: оплачен по данным Robokassa от {state.state_date}")

    if send_email and not updated.delivered_at:
        deliver(updated, orders_repo=orders_repo, runtime=runtime, site=site)
    return "paid"


def deliver(order: Order, *, orders_repo: OrdersRepository, runtime: RuntimeConfig, site: SiteConfig) -> None:
    """Дослать письмо с доступом. Неудача письма не отменяет оплату."""
    product = get_product(order.product_code) or get_product(DEFAULT_PRODUCT_CODE)
    if product is None:
        print(f"     письмо не отправлено: продукт {order.product_code} не найден в каталоге")
        return
    try:
        sent = send_order_email(smtp=runtime.smtp, site=site, product=product, order=order)
    except Exception as error:  # pragma: no cover - почта может быть недоступна
        print(f"     письмо не ушло: {error!r}. Ссылка: {order.access_token[:6]}…")
        return
    if sent:
        orders_repo.mark_delivered(order.order_id)
        print("     письмо с доступом отправлено")
    else:
        print("     SMTP не настроен — письмо не отправлено, выдайте ссылку вручную")


if __name__ == "__main__":
    sys.exit(main())
