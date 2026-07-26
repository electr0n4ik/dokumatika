"""Общая настройка тестов.

Тесты не поднимают сервер, не ходят в сеть и не требуют настроенного окружения:
база — временный файл, конфиги собираются вручную. Единственное, что нужно, —
положить ``src`` в путь импорта.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.config import RuntimeConfig, SellerConfig, SiteConfig  # noqa: E402
from app.db import Database  # noqa: E402
from app.repositories.metrics_repository import MetricsRepository  # noqa: E402
from app.repositories.orders_repository import OrdersRepository  # noqa: E402
from app.robokassa import RobokassaConfig  # noqa: E402


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.sqlite3")
    yield db
    db.close()


@pytest.fixture()
def orders(database: Database) -> OrdersRepository:
    repo = OrdersRepository(database)
    repo.ensure_schema()
    return repo


@pytest.fixture()
def metrics(database: Database) -> MetricsRepository:
    repo = MetricsRepository(database)
    repo.ensure_schema()
    return repo


@pytest.fixture()
def site() -> SiteConfig:
    return SiteConfig(
        domain="dokumatika.ru",
        metrika_id="",
        support_email="hello@dokumatika.ru",
        seller=SellerConfig(
            legal_form="Самозанятый",
            name="Иванов Иван Иванович",
            inn="770123456789",
            email="hello@dokumatika.ru",
            address="Москва",
        ),
    )


@pytest.fixture()
def runtime(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(database_path=tmp_path / "test.sqlite3", admin_token="secret-token")


@pytest.fixture()
def robokassa() -> RobokassaConfig:
    return RobokassaConfig(
        merchant_login="demo",
        password1="pass1",
        password2="pass2",
        test_password1="tpass1",
        test_password2="tpass2",
        test_mode=False,
        hash_algorithm="sha256",
    )
