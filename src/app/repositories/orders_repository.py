"""Заказы и их платёжный жизненный цикл.

Что здесь хранится и, главное, чего здесь НЕТ.

Хранится минимум: код продукта, сумма, email для доставки, статус оплаты и
идентификаторы Robokassa. Ответы визарда, реквизиты компании и сами документы
не покидают браузер покупателя — сервер их не видит и не пишет. Это не только
приватность: не будучи оператором чужих ПД по этим данным, сайт не обязан
хранить, защищать и удалять их по запросу.

Машина состояний:

    created ──► pending ──► paid  (единственный необратимый статус)
       │           ├──────► failed   ──┐
       └───────────┴──────► canceled ──┴──► paid

Необратим только ``paid``: Robokassa может прислать ResultURL раньше, чем
пользователь вернётся на SuccessURL, и повторить его несколько раз — оплаченный
заказ не должен «разоплатиться».

А вот ``canceled`` и ``failed`` обязаны пропускать оплату вперёд, и это не
теоретическая тонкость. Покупатель платит, ResultURL задерживается на секунды, в
это время покупатель жмёт «вернуться в магазин» — приходит FailURL. Если бы
отмена была необратимой, следом пришедший подтверждённый ResultURL был бы
отброшен: деньги у нас, документов у покупателя нет, а в админке заказ выглядит
просто отменённым. Подписанный платёж всегда сильнее отмены со стороны браузера.

Go migration notes:
- Соответствует internal/repository/orders; статусы и таблицу идемпотентности
  сохранить как есть.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL UNIQUE,
    product_code  TEXT NOT NULL,
    amount_minor  INTEGER NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'RUB',
    email         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'created',
    provider      TEXT NOT NULL DEFAULT 'robokassa',
    invoice_id    TEXT NOT NULL DEFAULT '',
    is_test       INTEGER NOT NULL DEFAULT 0,
    metadata      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    paid_at       TEXT,
    delivered_at  TEXT
);

CREATE INDEX IF NOT EXISTS orders_status_idx     ON orders(status);
CREATE INDEX IF NOT EXISTS orders_invoice_idx    ON orders(invoice_id);
CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders(created_at);

-- RU: Идемпотентность колбэков. Robokassa повторяет ResultURL, пока не получит
-- OK<InvId>; вставка сюда — «этот конкретный колбэк уже обработан».
CREATE TABLE IF NOT EXISTS order_webhook_events (
    event_id   TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

STATUS_CREATED = "created"
STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

TERMINAL_STATUSES = frozenset({STATUS_PAID, STATUS_FAILED, STATUS_CANCELED})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_CREATED: frozenset({STATUS_CREATED, STATUS_PENDING, STATUS_PAID, STATUS_FAILED, STATUS_CANCELED}),
    STATUS_PENDING: frozenset({STATUS_PENDING, STATUS_PAID, STATUS_FAILED, STATUS_CANCELED}),
    # RU: Из paid выхода нет — оплату нельзя отменить поздним колбэком.
    STATUS_PAID: frozenset({STATUS_PAID}),
    # RU: А из failed/canceled — есть, и только в paid: подтверждённый платёж
    # сильнее отказа банка и отмены в браузере.
    STATUS_FAILED: frozenset({STATUS_FAILED, STATUS_PAID}),
    STATUS_CANCELED: frozenset({STATUS_CANCELED, STATUS_PAID}),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Order:
    order_id: str
    access_token: str
    product_code: str
    amount_minor: int
    currency: str
    email: str
    status: str
    provider: str
    invoice_id: str
    is_test: bool
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    paid_at: str | None
    delivered_at: str | None

    @property
    def is_paid(self) -> bool:
        return self.status == STATUS_PAID

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class OrdersRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def ensure_schema(self) -> None:
        self._db.ensure_schema(SCHEMA)

    # ------------------------------------------------------------ создание

    @staticmethod
    def new_order_id() -> str:
        return f"ord_{secrets.token_urlsafe(12)}"

    @staticmethod
    def new_access_token() -> str:
        # RU: 32 байта энтропии — токен служит и ссылкой на заказ, и «ключом»
        # к оплаченному контенту, перебор исключён.
        return secrets.token_urlsafe(32)

    def create_order(
        self,
        *,
        product_code: str,
        amount_minor: int,
        email: str,
        invoice_id: str,
        is_test: bool,
        metadata: dict[str, Any] | None = None,
        currency: str = "RUB",
    ) -> Order:
        order_id = self.new_order_id()
        access_token = self.new_access_token()
        now = _now()
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, access_token, product_code, amount_minor, currency,
                    email, status, provider, invoice_id, is_test, metadata,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'robokassa', ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    access_token,
                    product_code,
                    int(amount_minor),
                    currency,
                    email,
                    STATUS_CREATED,
                    invoice_id,
                    1 if is_test else 0,
                    payload,
                    now,
                    now,
                ),
            )
        return Order(
            order_id=order_id,
            access_token=access_token,
            product_code=product_code,
            amount_minor=int(amount_minor),
            currency=currency,
            email=email,
            status=STATUS_CREATED,
            provider="robokassa",
            invoice_id=invoice_id,
            is_test=bool(is_test),
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
            paid_at=None,
            delivered_at=None,
        )

    # ------------------------------------------------------------- чтение

    def get_by_id(self, order_id: str) -> Order | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
        return self._row_to_order(row)

    def get_by_access_token(self, access_token: str) -> Order | None:
        token = str(access_token or "").strip()
        if not token:
            return None
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM orders WHERE access_token = ? LIMIT 1", (token,)).fetchone()
        return self._row_to_order(row)

    def get_by_invoice_id(self, invoice_id: str) -> Order | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT * FROM orders WHERE invoice_id = ? LIMIT 1", (invoice_id,)).fetchone()
        return self._row_to_order(row)

    def recent(self, limit: int = 50) -> list[Order]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [order for order in (self._row_to_order(row) for row in rows) if order is not None]

    def stats(self) -> dict[str, Any]:
        """Сводка для админки: сколько заказов и денег по статусам.

        Выручка считается ТОЛЬКО по боевым заказам (``is_test = 0``). При забытом
        ``ROBOKASSA_TEST_MODE=1`` тестовая оплата проходит без списания денег, и в
        общей строке она показала бы владельцу выручку, которой не существует.
        Тестовые оплаты возвращаются отдельными полями — чтобы забытый режим было
        видно сразу, а не по расхождению с выпиской.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT status, is_test, COUNT(*) AS cnt, COALESCE(SUM(amount_minor), 0) AS total
                FROM orders GROUP BY status, is_test
                """
            ).fetchall()
        by_status: dict[str, dict[str, int]] = {}
        live = {"count": 0, "amount_minor": 0}
        test = {"count": 0, "amount_minor": 0}
        for row in rows:
            status = str(row["status"])
            bucket = by_status.setdefault(status, {"count": 0, "amount_minor": 0})
            bucket["count"] += int(row["cnt"])
            bucket["amount_minor"] += int(row["total"])
            if status != STATUS_PAID:
                continue
            target = test if bool(row["is_test"]) else live
            target["count"] += int(row["cnt"])
            target["amount_minor"] += int(row["total"])
        return {
            "by_status": by_status,
            "paid_count": live["count"],
            "paid_amount_minor": live["amount_minor"],
            "test_paid_count": test["count"],
            "test_paid_amount_minor": test["amount_minor"],
        }

    # ------------------------------------------------------------ переходы

    def mark_pending(self, order_id: str) -> Order | None:
        """Пользователь ушёл на платёжную страницу. Не подтверждает оплату."""
        return self._transition(order_id, STATUS_PENDING)

    def mark_canceled(self, order_id: str) -> Order | None:
        return self._transition(order_id, STATUS_CANCELED)

    def _transition(self, order_id: str, status: str, metadata_patch: dict[str, Any] | None = None) -> Order | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
            current = self._row_to_order(row)
            if current is None:
                return None
            if status not in ALLOWED_TRANSITIONS.get(current.status, frozenset({current.status})):
                return current
            metadata = dict(current.metadata)
            if metadata_patch:
                metadata.update(metadata_patch)
            now = _now()
            paid_at = now if status == STATUS_PAID else current.paid_at
            conn.execute(
                "UPDATE orders SET status = ?, metadata = ?, updated_at = ?, paid_at = ? WHERE order_id = ?",
                (status, json.dumps(metadata, ensure_ascii=False), now, paid_at, order_id),
            )
            updated = conn.execute("SELECT * FROM orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
        return self._row_to_order(updated)

    def apply_paid_callback(
        self,
        *,
        event_id: str,
        order_id: str,
        metadata_patch: dict[str, Any] | None = None,
    ) -> tuple[Order | None, bool]:
        """Отметить заказ оплаченным ровно один раз.

        Возвращает ``(заказ, применён_ли_переход)``. ``applied=False`` при
        повторном колбэке — вызывающий код по этому признаку решает, слать ли
        письмо второй раз (не слать).
        """
        with self._db.transaction() as conn:
            seen = conn.execute(
                "SELECT order_id FROM order_webhook_events WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone()
            row = conn.execute("SELECT * FROM orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
            current = self._row_to_order(row)
            if current is None:
                # RU: Не расходуем event_id на несуществующий заказ — вдруг это
                # гонка с созданием, и повтор колбэка должен сработать.
                return None, False
            if seen is not None:
                return current, False

            if current.status == STATUS_PAID:
                # RU: Уже оплачен — независимо от event_id. Иначе колбэк с новым
                # идентификатором (например, из фоновой сверки) переписал бы
                # paid_at и отправил покупателю второе письмо.
                return current, False

            if STATUS_PAID not in ALLOWED_TRANSITIONS.get(current.status, frozenset({current.status})):
                # RU: event_id не расходуем: переход отклонён, а не обработан.
                # Иначе повторный колбэк по тому же событию был бы уже бесполезен.
                return current, False

            conn.execute(
                "INSERT INTO order_webhook_events (event_id, order_id, created_at) VALUES (?, ?, ?)",
                (event_id, order_id, _now()),
            )
            metadata = dict(current.metadata)
            if metadata_patch:
                metadata.update(metadata_patch)
            now = _now()
            conn.execute(
                "UPDATE orders SET status = ?, metadata = ?, updated_at = ?, paid_at = ? WHERE order_id = ?",
                (STATUS_PAID, json.dumps(metadata, ensure_ascii=False), now, now, order_id),
            )
            updated = conn.execute("SELECT * FROM orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
        return self._row_to_order(updated), True

    def mark_delivered(self, order_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE orders SET delivered_at = ? WHERE order_id = ?", (_now(), order_id))

    # ------------------------------------------------------------- служебное

    @staticmethod
    def _row_to_order(row: Any) -> Order | None:
        if row is None:
            return None
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return Order(
            order_id=str(row["order_id"]),
            access_token=str(row["access_token"]),
            product_code=str(row["product_code"]),
            amount_minor=int(row["amount_minor"]),
            currency=str(row["currency"]),
            email=str(row["email"] or ""),
            status=str(row["status"]),
            provider=str(row["provider"]),
            invoice_id=str(row["invoice_id"] or ""),
            is_test=bool(row["is_test"]),
            metadata=metadata,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            paid_at=row["paid_at"],
            delivered_at=row["delivered_at"],
        )
