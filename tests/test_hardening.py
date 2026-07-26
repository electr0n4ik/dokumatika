"""Тесты защитных правок вне HTTP-слоя.

Пул соединений после сбойного COMMIT, кардинальность меток воронки, разделение
боевой и тестовой выручки и московское время в ``ExpirationDate``. Каждый тест
падал бы до соответствующей правки.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import Database
from app.handlers import build_admin_page
from app.products import KOMPLEKT_152FZ
from app.repositories.metrics_repository import KNOWN_EVENTS, KNOWN_LABELS, MetricsRepository
from app.repositories.orders_repository import OrdersRepository
from app.robokassa import build_expiration_date


class FailingCommitConnection(sqlite3.Connection):
    """Соединение, у которого COMMIT падает — так ведёт себя полный диск."""

    fail_commit = False

    def execute(self, sql, *args):  # type: ignore[override] # noqa: D102
        if FailingCommitConnection.fail_commit and str(sql).strip().upper().startswith("COMMIT"):
            raise sqlite3.OperationalError("database or disk is full")
        return super().execute(sql, *args)


class TestConnectionPool:
    def test_open_transaction_is_not_returned_to_pool(self, tmp_path: Path) -> None:
        """Соединение с недозакрытой транзакцией отравляло весь пул до рестарта."""
        db = Database(tmp_path / "pool.sqlite3", pool_size=1)
        db.ensure_schema("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        try:
            with db.read() as conn:
                conn.execute("BEGIN IMMEDIATE")
            with db.transaction() as conn:
                conn.execute("INSERT INTO t (id) VALUES (1)")
            with db.read() as conn:
                assert conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"] == 1
        finally:
            db.close()

    def test_failed_commit_does_not_poison_pool(self, tmp_path: Path, monkeypatch) -> None:
        """Одна ошибка диска не должна ломать ВСЕ записи до перезапуска процесса."""
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            kwargs.setdefault("factory", FailingCommitConnection)
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", connect)
        db = Database(tmp_path / "commit.sqlite3", pool_size=1)
        db.ensure_schema("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        try:
            FailingCommitConnection.fail_commit = True
            with pytest.raises(sqlite3.OperationalError), db.transaction() as conn:
                conn.execute("INSERT INTO t (id) VALUES (1)")
            FailingCommitConnection.fail_commit = False
            # RU: Раньше здесь было «cannot start a transaction within a transaction».
            with db.transaction() as conn:
                conn.execute("INSERT INTO t (id) VALUES (2)")
            with db.read() as conn:
                rows = [int(row["id"]) for row in conn.execute("SELECT id FROM t").fetchall()]
            assert rows == [2], "незакоммиченная строка не должна была уцелеть"
        finally:
            FailingCommitConnection.fail_commit = False
            db.close()


class TestFunnelLabels:
    def test_unknown_labels_collapse_into_one_row(self, metrics: MetricsRepository) -> None:
        """Скрипт с уникальной меткой в каждом запросе раздувал funnel_counters."""
        for index in range(50):
            assert metrics.track("wizard_step", f"u-{index}")
        rows = [row for row in metrics.daily() if row.event == "wizard_step"]
        assert len(rows) == 1
        assert rows[0].label == "other" and rows[0].count == 50

    def test_known_labels_survive(self, metrics: MetricsRepository) -> None:
        metrics.track("wizard_step", "step-3")
        metrics.track("package_download", "cookie_policy")
        labels = {(row.event, row.label) for row in metrics.daily()}
        assert ("wizard_step", "step-3") in labels
        assert ("package_download", "cookie_policy") in labels

    def test_empty_label_stays_empty(self, metrics: MetricsRepository) -> None:
        assert MetricsRepository.resolve("wizard_start") == ("wizard_start", "")

    def test_unknown_event_still_rejected(self, metrics: MetricsRepository) -> None:
        assert MetricsRepository.resolve("evil_event", "step-1") is None

    def test_label_list_covers_every_event(self) -> None:
        """Новое событие без списка меток тихо потеряло бы все свои метки в other."""
        assert set(KNOWN_LABELS) == set(KNOWN_EVENTS)


class TestRevenueSplit:
    def _order(self, orders: OrdersRepository, *, invoice_id: str, is_test: bool):
        return orders.create_order(
            product_code=KOMPLEKT_152FZ.code,
            amount_minor=KOMPLEKT_152FZ.amount_minor,
            email="buyer@example.com",
            invoice_id=invoice_id,
            is_test=is_test,
        )

    def test_test_payments_are_not_revenue(self, orders: OrdersRepository) -> None:
        """Забытый ROBOKASSA_TEST_MODE=1 показывал владельцу несуществующие деньги."""
        live = self._order(orders, invoice_id="1", is_test=False)
        test = self._order(orders, invoice_id="2", is_test=True)
        orders.apply_paid_callback(event_id="a", order_id=live.order_id)
        orders.apply_paid_callback(event_id="b", order_id=test.order_id)

        stats = orders.stats()
        assert stats["paid_count"] == 1
        assert stats["paid_amount_minor"] == KOMPLEKT_152FZ.amount_minor
        assert stats["test_paid_count"] == 1
        assert stats["test_paid_amount_minor"] == KOMPLEKT_152FZ.amount_minor
        # RU: Общая разбивка по статусам считает оба заказа — она про заказы, не про деньги.
        assert stats["by_status"]["paid"]["count"] == 2

    def test_admin_page_shows_test_payments_apart(self, orders: OrdersRepository, site) -> None:
        test = self._order(orders, invoice_id="3", is_test=True)
        orders.apply_paid_callback(event_id="c", order_id=test.order_id)
        _, body = build_admin_page(
            site=site,
            orders=orders.recent(10),
            stats=orders.stats(),
            funnel={},
            payments_enabled=True,
            robokassa_configured=True,
            test_mode=True,
        )
        html = str(body)
        assert "Включён тестовый режим Robokassa" in html
        assert "Тестовые оплаты (не выручка)" in html
        # RU: Выручка — ноль: единственная оплата была тестовой.
        assert "<strong>0 ₽</strong>" in html


class TestExpirationDate:
    def test_counted_in_moscow_time(self) -> None:
        """Наивную строку Robokassa читает как МСК — счёт умирал на 3 часа раньше."""
        now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        assert build_expiration_date(24, now=now) == "2026-07-27T15:00"

    def test_short_ttl_stays_in_future(self) -> None:
        """При hours <= 3 расчёт в UTC давал дату в прошлом и ошибку 33."""
        now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        moscow_now = "2026-07-26T15:00"
        assert build_expiration_date(1, now=now) > moscow_now

    def test_naive_now_treated_as_utc(self) -> None:
        naive = datetime(2026, 7, 26, 12, 0)
        assert build_expiration_date(24, now=naive) == "2026-07-27T15:00"
