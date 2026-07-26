"""Тесты подписи и протокола Robokassa.

Самая дорогая ошибка проекта живёт здесь: неверная подпись — и оплата не
проходит вовсе, а слишком доверчивая проверка — и товар отдаётся бесплатно.
Поэтому формулы зафиксированы тестами на официальных примерах из документации.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import unquote

import pytest

from app.robokassa import (
    ROBOKASSA_CHECKOUT_URL,
    RobokassaConfig,
    amount_matches_minor,
    build_checkout_form,
    build_expiration_date,
    build_receipt,
    build_signature_base,
    collect_shp_params,
    encode_receipt,
    format_amount_minor,
    hash_signature,
    new_invoice_id,
    result_ok_response,
    verify_result_callback,
    verify_success_callback,
)


class TestSignatureBase:
    def test_official_receipt_example_order(self) -> None:
        """Документация: MerchantLogin:OutSum:InvId:Receipt:Пароль#1:Shp_order=25."""
        base = build_signature_base(
            "demo",
            "8.96",
            "5",
            "RECEIPT",
            "password_1",
            shp_params=[("Shp_order", "25")],
        )
        assert base == "demo:8.96:5:RECEIPT:password_1:Shp_order=25"

    def test_result_example_order(self) -> None:
        """Документация: OutSum:InvId:Пароль#2:Shp_login=Vasya:Shp_oplata=1."""
        base = build_signature_base(
            "100.000000",
            "450009",
            "password_2",
            shp_params=[("Shp_login", "Vasya"), ("Shp_oplata", "1")],
        )
        assert base == "100.000000:450009:password_2:Shp_login=Vasya:Shp_oplata=1"

    def test_shp_params_sorted_alphabetically(self) -> None:
        """Robokassa подписывает Shp_* в алфавитном порядке независимо от порядка передачи."""
        pairs = collect_shp_params({"Shp_zebra": "1", "Shp_alpha": "2", "OutSum": "10", "Shp_beta": "3"})
        assert pairs == [("Shp_alpha", "2"), ("Shp_beta", "3"), ("Shp_zebra", "1")]

    def test_non_shp_params_excluded(self) -> None:
        assert collect_shp_params({"OutSum": "10", "InvId": "5"}) == []


class TestHashing:
    @pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha256", "sha384", "sha512"])
    def test_matches_hashlib(self, algorithm: str) -> None:
        value = "demo:8.96:5:password_1"
        expected = hashlib.new(algorithm, value.encode("utf-8")).hexdigest()
        assert hash_signature(value, algorithm) == expected

    def test_rejects_unknown_algorithm(self) -> None:
        with pytest.raises(ValueError):
            hash_signature("x", "crc32")


class TestAmounts:
    def test_format_minor_to_two_decimals(self) -> None:
        assert format_amount_minor(79900) == "799.00"
        assert format_amount_minor(1) == "0.01"

    def test_production_result_sends_six_decimals(self) -> None:
        """В бою OutSum приходит как 799.000000 — сверка не должна ломаться."""
        assert amount_matches_minor("799.000000", 79900)

    def test_accepts_comma_separator(self) -> None:
        assert amount_matches_minor("799,00", 79900)

    def test_rejects_wrong_amount(self) -> None:
        assert not amount_matches_minor("1.00", 79900)

    def test_rejects_garbage(self) -> None:
        assert not amount_matches_minor("много", 79900)


class TestReceipt:
    def test_sum_equals_order_amount(self) -> None:
        receipt = build_receipt("Комплект", 79900)
        item = receipt["items"][0]  # type: ignore[index]
        assert item["sum"] == 799
        assert item["tax"] == "none"

    def test_kopecks_kept_as_float(self) -> None:
        receipt = build_receipt("Комплект", 79950)
        assert receipt["items"][0]["sum"] == 799.5  # type: ignore[index]

    def test_encoded_receipt_is_url_encoded_json(self) -> None:
        encoded = encode_receipt(build_receipt("Комплект", 79900))
        assert "%7B" in encoded and " " not in encoded
        restored = json.loads(unquote(encoded))
        assert restored["items"][0]["quantity"] == 1

    def test_name_truncated_to_limit(self) -> None:
        receipt = build_receipt("я" * 300, 79900)
        assert len(receipt["items"][0]["name"]) == 128  # type: ignore[index]


class TestCheckoutForm:
    def test_signature_matches_manual_computation(self, robokassa: RobokassaConfig) -> None:
        form = build_checkout_form(
            config=robokassa,
            invoice_id="123",
            order_id="ord_abc",
            amount_minor=79900,
            description="Комплект документов",
            email="user@example.com",
        )
        fields = form["fields"]
        expected_base = build_signature_base(
            "demo",
            "799.00",
            "123",
            fields["Receipt"],
            "pass1",
            shp_params=[("Shp_order_id", "ord_abc")],
        )
        assert fields["SignatureValue"] == hash_signature(expected_base, "sha256")
        assert form["action"] == ROBOKASSA_CHECKOUT_URL
        assert form["method"] == "POST"

    def test_test_mode_uses_test_password_and_flag(self, robokassa: RobokassaConfig) -> None:
        form = build_checkout_form(
            config=robokassa,
            invoice_id="123",
            order_id="ord_abc",
            amount_minor=79900,
            description="Комплект",
            is_test=True,
        )
        fields = form["fields"]
        assert fields["IsTest"] == "1"
        expected_base = build_signature_base(
            "demo", "799.00", "123", fields["Receipt"], "tpass1", shp_params=[("Shp_order_id", "ord_abc")]
        )
        assert fields["SignatureValue"] == hash_signature(expected_base, "sha256")

    def test_test_mode_without_test_passwords_fails_loudly(self) -> None:
        config = RobokassaConfig(merchant_login="demo", password1="p1", password2="p2")
        with pytest.raises(ValueError):
            build_checkout_form(
                config=config,
                invoice_id="1",
                order_id="ord",
                amount_minor=100,
                description="x",
                is_test=True,
            )

    def test_description_truncated(self, robokassa: RobokassaConfig) -> None:
        form = build_checkout_form(
            config=robokassa,
            invoice_id="1",
            order_id="ord",
            amount_minor=100,
            description="д" * 250,
        )
        assert len(form["fields"]["Description"]) == 100

    def test_email_omitted_when_empty(self, robokassa: RobokassaConfig) -> None:
        form = build_checkout_form(
            config=robokassa, invoice_id="1", order_id="ord", amount_minor=100, description="x"
        )
        assert "Email" not in form["fields"]

    def test_expiration_date_format(self) -> None:
        value = build_expiration_date(24)
        assert len(value) == 16 and value[10] == "T"


class TestInvoiceId:
    def test_numeric_and_within_int64(self) -> None:
        invoice = new_invoice_id()
        assert invoice.isdigit()
        assert 0 < int(invoice) < 9223372036854775807

    def test_unique_across_calls(self) -> None:
        assert len({new_invoice_id() for _ in range(200)}) == 200


def _sign_result(config: RobokassaConfig, out_sum: str, invoice_id: str, order_id: str, password: str) -> str:
    return hash_signature(
        build_signature_base(out_sum, invoice_id, password, shp_params=[("Shp_order_id", order_id)]),
        config.hash_algorithm,
    )


class TestResultVerification:
    def _payload(
        self, config: RobokassaConfig, *, out_sum: str = "799.000000", password: str = "pass2"
    ) -> dict:
        return {
            "OutSum": out_sum,
            "InvId": "123",
            "Shp_order_id": "ord_abc",
            "SignatureValue": _sign_result(config, out_sum, "123", "ord_abc", password),
        }

    def test_accepts_valid_callback(self, robokassa: RobokassaConfig) -> None:
        result = verify_result_callback(
            self._payload(robokassa),
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=False,
        )
        assert result.ok and result.order_id == "ord_abc"

    def test_uppercase_signature_accepted(self, robokassa: RobokassaConfig) -> None:
        payload = self._payload(robokassa)
        payload["SignatureValue"] = payload["SignatureValue"].upper()
        result = verify_result_callback(
            payload,
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=False,
        )
        assert result.ok

    def test_rejects_tampered_amount(self, robokassa: RobokassaConfig) -> None:
        """Подмена суммы в форме не должна оплачивать товар дешевле."""
        payload = self._payload(robokassa, out_sum="1.00")
        result = verify_result_callback(
            payload,
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=False,
        )
        assert not result.ok and result.reason == "amount_mismatch"

    def test_rejects_wrong_signature(self, robokassa: RobokassaConfig) -> None:
        payload = self._payload(robokassa)
        payload["SignatureValue"] = "0" * 64
        result = verify_result_callback(
            payload,
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=False,
        )
        assert not result.ok and result.reason == "invalid_signature"

    def test_rejects_signature_made_with_password1(self, robokassa: RobokassaConfig) -> None:
        """ResultURL подписывается Паролем #2 — подпись первым не должна проходить."""
        payload = self._payload(robokassa, password="pass1")
        result = verify_result_callback(
            payload,
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=False,
        )
        assert not result.ok and result.reason == "invalid_signature"

    def test_rejects_invoice_mismatch(self, robokassa: RobokassaConfig) -> None:
        result = verify_result_callback(
            self._payload(robokassa),
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="999",
            is_test=False,
        )
        assert not result.ok and result.reason == "invoice_mismatch"

    def test_rejects_missing_parameters(self, robokassa: RobokassaConfig) -> None:
        result = verify_result_callback(
            {"OutSum": "799.00"},
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=False,
        )
        assert not result.ok and result.reason == "missing_parameters"

    def test_test_mode_uses_test_password2(self, robokassa: RobokassaConfig) -> None:
        payload = self._payload(robokassa, out_sum="799.00", password="tpass2")
        result = verify_result_callback(
            payload,
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=True,
        )
        assert result.ok

    def test_test_callback_rejected_with_production_password(self, robokassa: RobokassaConfig) -> None:
        """Боевой и тестовый трафик не должны путаться."""
        payload = self._payload(robokassa, out_sum="799.00", password="pass2")
        result = verify_result_callback(
            payload,
            config=robokassa,
            expected_amount_minor=79900,
            expected_invoice_id="123",
            is_test=True,
        )
        assert not result.ok


class TestSuccessVerification:
    def test_success_uses_password1(self, robokassa: RobokassaConfig) -> None:
        payload = {
            "OutSum": "799.00",
            "InvId": "123",
            "Shp_order_id": "ord_abc",
            "SignatureValue": _sign_result(robokassa, "799.00", "123", "ord_abc", "pass1"),
        }
        assert verify_success_callback(payload, config=robokassa, is_test=False).ok

    def test_success_rejects_password2_signature(self, robokassa: RobokassaConfig) -> None:
        payload = {
            "OutSum": "799.00",
            "InvId": "123",
            "Shp_order_id": "ord_abc",
            "SignatureValue": _sign_result(robokassa, "799.00", "123", "ord_abc", "pass2"),
        }
        assert not verify_success_callback(payload, config=robokassa, is_test=False).ok


def test_result_ok_response_is_exact() -> None:
    """Robokassa ждёт ровно OK<InvId> без пробелов и переводов строки."""
    assert result_ok_response("450009") == "OK450009"
