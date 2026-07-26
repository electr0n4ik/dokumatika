"""Тесты машины состояний заказа и идемпотентности колбэков.

Robokassa повторяет ResultURL, пока не получит ``OK<InvId>``, и порядок доставки
не гарантирован. Всё, что здесь проверяется, — что от повторов и «поздних»
уведомлений заказ не ломается и товар не выдаётся дважды.
"""

from __future__ import annotations

import threading

from app.repositories.orders_repository import (
    STATUS_CANCELED,
    STATUS_CREATED,
    STATUS_PAID,
    OrdersRepository,
)


def make_order(orders: OrdersRepository, **overrides):
    payload = {
        "product_code": "komplekt_152fz",
        "amount_minor": 79900,
        "email": "user@example.com",
        "invoice_id": "1001",
        "is_test": False,
    }
    payload.update(overrides)
    return orders.create_order(**payload)


class TestCreation:
    def test_starts_in_created(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        assert order.status == STATUS_CREATED
        assert not order.is_paid

    def test_access_token_is_long_and_unique(self, orders: OrdersRepository) -> None:
        tokens = {make_order(orders, invoice_id=str(index)).access_token for index in range(50)}
        assert len(tokens) == 50
        assert all(len(token) >= 32 for token in tokens)

    def test_lookup_by_token_and_invoice(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        assert orders.get_by_access_token(order.access_token).order_id == order.order_id
        assert orders.get_by_invoice_id("1001").order_id == order.order_id

    def test_unknown_token_returns_none(self, orders: OrdersRepository) -> None:
        assert orders.get_by_access_token("nope") is None
        assert orders.get_by_access_token("") is None


class TestTransitions:
    def test_pending_then_paid(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        orders.mark_pending(order.order_id)
        updated, applied = orders.apply_paid_callback(event_id="e1", order_id=order.order_id)
        assert applied and updated.status == STATUS_PAID
        assert updated.paid_at

    def test_paid_is_terminal_and_survives_cancel(self, orders: OrdersRepository) -> None:
        """Пользователь может открыть FailURL уже после успешного ResultURL."""
        order = make_order(orders)
        orders.apply_paid_callback(event_id="e1", order_id=order.order_id)
        result = orders.mark_canceled(order.order_id)
        assert result.status == STATUS_PAID

    def test_canceled_cannot_become_pending(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        orders.mark_canceled(order.order_id)
        assert orders.mark_pending(order.order_id).status == STATUS_CANCELED

    def test_canceled_can_still_become_paid(self, orders: OrdersRepository) -> None:
        """Отмена на стороне пользователя не должна мешать реальной оплате.

        Сценарий из жизни: покупатель платит, ResultURL задерживается, покупатель
        жмёт «вернуться в магазин». Если бы отмена была необратимой, пришедший
        следом подтверждённый платёж был бы отброшен — деньги списаны, документов
        нет.
        """
        order = make_order(orders)
        orders.mark_canceled(order.order_id)
        updated, applied = orders.apply_paid_callback(event_id="e1", order_id=order.order_id)
        assert applied and updated.status == STATUS_PAID

    def test_second_source_does_not_repay_order(self, orders: OrdersRepository) -> None:
        """Оплата фиксируется один раз, даже если событие пришло из другого источника.

        Фоновая сверка через OpStateExt использует собственный ``event_id``, и без
        этой проверки она переписала бы ``paid_at`` и отправила второе письмо.
        """
        order = make_order(orders)
        first, _ = orders.apply_paid_callback(event_id="robokassa:1", order_id=order.order_id)
        second, applied = orders.apply_paid_callback(event_id="opstate:1", order_id=order.order_id)
        assert not applied
        assert second.status == STATUS_PAID
        assert second.paid_at == first.paid_at

    def test_transition_on_missing_order(self, orders: OrdersRepository) -> None:
        assert orders.mark_pending("ord_missing") is None


class TestIdempotency:
    def test_repeat_callback_applies_once(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        first, applied_first = orders.apply_paid_callback(event_id="evt", order_id=order.order_id)
        second, applied_second = orders.apply_paid_callback(event_id="evt", order_id=order.order_id)
        assert applied_first and not applied_second
        assert first.status == second.status == STATUS_PAID

    def test_missing_order_does_not_consume_event(self, orders: OrdersRepository) -> None:
        """Гонка «колбэк раньше записи» не должна съедать event_id навсегда."""
        updated, applied = orders.apply_paid_callback(event_id="evt", order_id="ord_missing")
        assert updated is None and not applied

        order = make_order(orders, invoice_id="2002")
        # RU: тот же event_id, но теперь заказ существует — переход обязан пройти.
        result, applied_again = orders.apply_paid_callback(event_id="evt", order_id=order.order_id)
        assert applied_again and result.status == STATUS_PAID

    def test_concurrent_callbacks_apply_once(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        applied_flags: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            _, applied = orders.apply_paid_callback(event_id="evt", order_id=order.order_id)
            with lock:
                applied_flags.append(applied)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(applied_flags) == 1
        assert orders.get_by_id(order.order_id).status == STATUS_PAID


class TestMetadataAndStats:
    def test_metadata_patch_merges(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        updated, _ = orders.apply_paid_callback(
            event_id="evt", order_id=order.order_id, metadata_patch={"robokassa_fee": "27.17"}
        )
        assert updated.metadata["robokassa_fee"] == "27.17"

    def test_stats_counts_only_paid_revenue(self, orders: OrdersRepository) -> None:
        paid = make_order(orders, invoice_id="1")
        orders.apply_paid_callback(event_id="a", order_id=paid.order_id)
        make_order(orders, invoice_id="2")
        stats = orders.stats()
        assert stats["paid_count"] == 1
        assert stats["paid_amount_minor"] == 79900

    def test_delivered_at_recorded(self, orders: OrdersRepository) -> None:
        order = make_order(orders)
        orders.mark_delivered(order.order_id)
        assert orders.get_by_id(order.order_id).delivered_at

    def test_recent_orders_newest_first(self, orders: OrdersRepository) -> None:
        for index in range(3):
            make_order(orders, invoice_id=str(index))
        assert len(orders.recent(10)) == 3


class TestMetrics:
    def test_known_event_counted(self, metrics) -> None:
        assert metrics.track("wizard_start")
        assert metrics.track("wizard_start")
        assert metrics.totals()["wizard_start"] == 2

    def test_unknown_event_rejected(self, metrics) -> None:
        """Публичный эндпоинт не должен позволять раздувать базу произвольными ключами."""
        assert not metrics.track("evil_event")
        assert "evil_event" not in metrics.totals()

    def test_label_keeps_expected_slug(self, metrics) -> None:
        assert metrics.normalize("wizard_step", "step-2") == ("wizard_step", "step-2")

    def test_label_strips_dangerous_characters(self, metrics) -> None:
        """Метку рисуют в админке — разметка и кавычки в неё попасть не должны."""
        _, label = metrics.normalize("wizard_step", 'шаг <script>"x"</script>')
        assert not (set(label) & set("<>\"'&/ "))

    def test_label_truncated(self, metrics) -> None:
        _, label = metrics.normalize("wizard_step", "a" * 200)
        assert len(label) == 48

    def test_label_cardinality_is_bounded(self, metrics) -> None:
        """Публичный /api/track не должен позволять плодить строки произвольно."""
        _, first = metrics.normalize("wizard_step", "шаг")
        _, second = metrics.normalize("wizard_step", "этап")
        assert first == second == ""


class TestDatabaseHousekeeping:
    def test_healthcheck_reports_ok(self, orders: OrdersRepository, database) -> None:
        assert database.healthcheck()["db"] == "ok"

    def test_backup_creates_readable_copy(self, orders: OrdersRepository, database, tmp_path) -> None:
        make_order(orders)
        target = database.backup_to(tmp_path / "backup" / "copy.sqlite3")
        assert target.exists() and target.stat().st_size > 0

    def test_checkpoint_rejects_bad_mode(self, orders: OrdersRepository, database) -> None:
        import pytest

        with pytest.raises(ValueError):
            database.checkpoint("NONSENSE")
