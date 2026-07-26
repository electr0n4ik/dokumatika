"""Интеграция с Robokassa.

Модуль портирован с боевой реализации проекта plantsChoise и адаптирован под
разовую продажу цифрового товара (без подписок и личных кабинетов).

Модель доверия — единственное, что здесь по-настоящему важно:

* **ResultURL — единственный источник правды.** Только этот запрос переводит
  заказ в оплаченный статус, и только после проверки подписи на Password #2.
* **SuccessURL ничего не подтверждает.** Пользователь может открыть его руками,
  подставив любые параметры. Страница успеха лишь показывает статус заказа,
  который к тому моменту уже выставил (или не выставил) ResultURL.
* **Сумма и инвойс сверяются с нашей записью**, а не берутся из запроса. Иначе
  подмена ``OutSum`` в форме оплатила бы товар за рубль.
* **Повторный ResultURL безопасен** — идемпотентность по ``event_id`` в
  репозитории платежей; Robokassa шлёт повтор, пока не получит ``OK<InvId>``.

Go migration notes:
- Соответствует internal/payments/robokassa; формулы подписи менять нельзя.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from .config import env_flag, env_str

# RU: Рабочая точка входа. В «Быстром старте» документации встречается
# .../Merchant/Payment/Index — он отдаёт 404, это ошибка в документации.
ROBOKASSA_CHECKOUT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"

# RU: Сервис проверки статуса платежа — страховка, если ResultURL не дошёл.
ROBOKASSA_OPSTATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"

# RU: Адреса, с которых Robokassa шлёт уведомления. Используются как ВТОРОЙ
# рубеж (allow в nginx), но никогда вместо проверки подписи.
ROBOKASSA_CALLBACK_IPS = ("185.59.216.65", "185.59.217.65")

# RU: Алгоритм должен совпадать с выбранным в ЛК («Мои магазины» → «Технические
# настройки»), иначе Robokassa вернёт ошибку 29. По умолчанию в кабинете стоит
# MD5 — при переключении на sha256 не забыть поменять и там, и в .env.
# RU: RIPEMD160 Robokassa поддерживает, но мы его не предлагаем: в сборках
# OpenSSL 3.x он часто отключён, и hashlib падает уже в момент оплаты. Лучше
# отсечь на этапе конфигурации, чем ловить 500 на боевом платеже.
ALLOWED_HASH_ALGORITHMS = frozenset({"md5", "sha1", "sha256", "sha384", "sha512"})

# RU: Значения фискального чека. Для самозанятого/НПД без НДС — tax="none".
# Товар цифровой и передаётся сразу -> full_payment + service.
ALLOWED_RECEIPT_PAYMENT_METHODS = frozenset(
    {"full_prepayment", "prepayment", "advance", "full_payment", "partial_payment", "credit", "credit_payment"}
)
ALLOWED_RECEIPT_PAYMENT_OBJECTS = frozenset(
    {
        "commodity",
        "excise",
        "job",
        "service",
        "gambling_bet",
        "gambling_prize",
        "lottery",
        "lottery_prize",
        "intellectual_activity",
        "payment",
        "agent_commission",
        "composite",
        "resort_fee",
        "another",
        "property_right",
        "non-operating_gain",
        "insurance_premium",
        "sales_tax",
        "tovar_mark",
    }
)
# RU: vat22/vat122 и vat5/vat7 добавлены под действующие ставки НДС.
ALLOWED_RECEIPT_TAXES = frozenset(
    {"none", "vat0", "vat5", "vat7", "vat10", "vat20", "vat22", "vat105", "vat107", "vat110", "vat120", "vat122"}
)

DEFAULT_RECEIPT_PAYMENT_METHOD = "full_payment"
DEFAULT_RECEIPT_PAYMENT_OBJECT = "service"
DEFAULT_RECEIPT_TAX = "none"


@dataclass(frozen=True)
class RobokassaConfig:
    merchant_login: str
    password1: str
    password2: str
    test_password1: str = ""
    test_password2: str = ""
    test_mode: bool = False
    hash_algorithm: str = "sha256"
    receipt_payment_method: str = DEFAULT_RECEIPT_PAYMENT_METHOD
    receipt_payment_object: str = DEFAULT_RECEIPT_PAYMENT_OBJECT
    receipt_tax: str = DEFAULT_RECEIPT_TAX

    def password1_for_mode(self, is_test: bool) -> str:
        return self.test_password1 if is_test else self.password1

    def password2_for_mode(self, is_test: bool) -> str:
        return self.test_password2 if is_test else self.password2

    def with_test_mode(self, test_mode: bool) -> "RobokassaConfig":
        return replace(self, test_mode=bool(test_mode))


def _normalize_enum(value: str, *, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def load_robokassa_config() -> RobokassaConfig | None:
    """Собрать конфиг из env. ``None`` = интеграция не настроена, оплата выключена.

    Тестовые пароли проверяются не здесь, а в момент построения формы: боевой
    режим не должен падать из-за незаполненных тестовых полей.
    """
    merchant_login = env_str("ROBOKASSA_MERCHANT_LOGIN")
    password1 = env_str("ROBOKASSA_PASSWORD1")
    password2 = env_str("ROBOKASSA_PASSWORD2")
    if not merchant_login or not password1 or not password2:
        return None
    return RobokassaConfig(
        merchant_login=merchant_login,
        password1=password1,
        password2=password2,
        test_password1=env_str("ROBOKASSA_TEST_PASSWORD1"),
        test_password2=env_str("ROBOKASSA_TEST_PASSWORD2"),
        test_mode=env_flag("ROBOKASSA_TEST_MODE", default=False),
        hash_algorithm=_normalize_enum(
            env_str("ROBOKASSA_HASH_ALGORITHM", "sha256"),
            allowed=ALLOWED_HASH_ALGORITHMS,
            default="sha256",
        ),
        receipt_payment_method=_normalize_enum(
            env_str("ROBOKASSA_RECEIPT_PAYMENT_METHOD", DEFAULT_RECEIPT_PAYMENT_METHOD),
            allowed=ALLOWED_RECEIPT_PAYMENT_METHODS,
            default=DEFAULT_RECEIPT_PAYMENT_METHOD,
        ),
        receipt_payment_object=_normalize_enum(
            env_str("ROBOKASSA_RECEIPT_PAYMENT_OBJECT", DEFAULT_RECEIPT_PAYMENT_OBJECT),
            allowed=ALLOWED_RECEIPT_PAYMENT_OBJECTS,
            default=DEFAULT_RECEIPT_PAYMENT_OBJECT,
        ),
        receipt_tax=_normalize_enum(
            env_str("ROBOKASSA_RECEIPT_TAX", DEFAULT_RECEIPT_TAX),
            allowed=ALLOWED_RECEIPT_TAXES,
            default=DEFAULT_RECEIPT_TAX,
        ),
    )


# --------------------------------------------------------------------- деньги


def format_amount_minor(amount_minor: int) -> str:
    """Копейки -> строка ``799.00``, как того ждёт ``OutSum``."""
    return f"{Decimal(int(amount_minor)) / Decimal('100'):.2f}"


def amount_matches_minor(amount_text: str, amount_minor: int) -> bool:
    """Сравнить пришедшую сумму с нашей записью. Запятая как разделитель допустима."""
    try:
        normalized = Decimal(str(amount_text).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return False
    return normalized == (Decimal(int(amount_minor)) / Decimal("100"))


def _receipt_sum(amount_minor: int) -> int | float:
    normalized = Decimal(int(amount_minor)) / Decimal("100")
    if normalized == normalized.to_integral():
        return int(normalized)
    return float(f"{normalized:.2f}")


# ------------------------------------------------------------------- подписи


def hash_signature(value: str, algorithm: str) -> str:
    if algorithm not in ALLOWED_HASH_ALGORITHMS:
        raise ValueError("unsupported_hash_algorithm")
    try:
        digest = hashlib.new(algorithm, value.encode("utf-8"))
    except ValueError as error:
        # RU: ripemd160 доступен не во всех сборках OpenSSL 3.x.
        raise ValueError("unsupported_hash_algorithm") from error
    return digest.hexdigest()


def collect_shp_params(values: dict[str, str]) -> list[tuple[str, str]]:
    """Собрать пользовательские ``Shp_*`` в алфавитном порядке.

    Порядок критичен: Robokassa подписывает их отсортированными по имени, и
    любое расхождение даёт «invalid signature» без внятной диагностики.
    """
    pairs = [(str(key), str(value)) for key, value in values.items() if str(key).startswith("Shp_")]
    pairs.sort(key=lambda item: item[0])
    return pairs


def build_signature_base(*parts: str, shp_params: list[tuple[str, str]] | None = None) -> str:
    base_parts = [str(part) for part in parts]
    base_parts.extend(f"{key}={value}" for key, value in (shp_params or []))
    return ":".join(base_parts)


_invoice_lock = threading.Lock()
_last_invoice_id = 0


def new_invoice_id() -> str:
    """Числовой ``InvId`` для Robokassa — строго уникальный и возрастающий.

    Повтор ``InvId`` стоит дорого: Robokassa вернёт ошибку 40 («повторная оплата
    счёта с тем же номером невозможна»), и покупатель просто не сможет заплатить.
    Одной метки времени с random-хвостом мало — два заказа в одну миллисекунду
    могут вытянуть одинаковый хвост, поэтому результат дополнительно
    протаскивается через счётчик под блокировкой.

    Внутренний идентификатор заказа остаётся строковым и едет в ``Shp_order_id``.
    """
    global _last_invoice_id
    candidate = int(time.time() * 1000) * 10_000 + secrets.randbelow(10_000)
    with _invoice_lock:
        # RU: После рестарта процесса счётчик обнуляется, но метка времени уже
        # ушла вперёд — монотонность сохраняется и между запусками.
        if candidate <= _last_invoice_id:
            candidate = _last_invoice_id + 1
        _last_invoice_id = candidate
    return str(candidate)


# --------------------------------------------------------------------- чек


def build_receipt(
    name: str,
    amount_minor: int,
    *,
    payment_method: str = DEFAULT_RECEIPT_PAYMENT_METHOD,
    payment_object: str = DEFAULT_RECEIPT_PAYMENT_OBJECT,
    tax: str = DEFAULT_RECEIPT_TAX,
) -> dict[str, object]:
    line_sum = _receipt_sum(amount_minor)
    return {
        "items": [
            {
                # RU: Название в чеке должно совпадать с тем, что человек купил
                # на витрине — иначе вопросы и от покупателя, и от ФНС.
                "name": name[:128],
                "quantity": 1,
                "sum": line_sum,
                "cost": line_sum,
                "payment_method": payment_method,
                "payment_object": payment_object,
                "tax": tax,
            }
        ]
    }


def encode_receipt(receipt: dict[str, object]) -> str:
    """Закодировать чек ровно так, как он поедет в форме и в подписи.

    Компактный JSON без пробелов + полный URL-encode. Подпись считается от этой
    же строки, поэтому любое изменение сериализации ломает оплату.
    """
    raw = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    return quote(raw, safe="")


# ----------------------------------------------------------------- checkout


def build_expiration_date(hours: int = 24, *, now: datetime | None = None) -> str:
    """``ExpirationDate`` в ISO 8601 без таймзоны, как ждёт Robokassa.

    Ограничение срока жизни ссылки — защита от оплаты по устаревшей цене:
    просроченная форма вернёт ошибку 33 вместо неожиданного платежа.
    """
    moment = (now or datetime.now(timezone.utc)) + timedelta(hours=int(hours))
    return moment.strftime("%Y-%m-%dT%H:%M")


def build_checkout_form(
    *,
    config: RobokassaConfig,
    invoice_id: str,
    order_id: str,
    amount_minor: int,
    description: str,
    email: str = "",
    is_test: bool = False,
    receipt: dict[str, object] | None = None,
    expiration_date: str = "",
) -> dict[str, object]:
    """Собрать POST-форму на платёжную страницу Robokassa.

    Именно форма, а не редирект: чек (``Receipt``) длинный, в GET-URL он упрётся
    в лимиты и осядет в логах; к тому же он участвует в подписи.

    Порядок слотов подписи фиксирован документацией и менять его нельзя:
    ``MerchantLogin:OutSum:InvId:Receipt:Пароль#1:Shp_*`` — модификаторы (здесь
    только ``Receipt``) идут между ``InvId`` и паролем, ``Shp_*`` — после пароля
    и строго по алфавиту.
    """
    if is_test and (not config.test_password1 or not config.test_password2):
        raise ValueError("robokassa_test_passwords_missing")

    payload = receipt if isinstance(receipt, dict) else build_receipt(
        description,
        amount_minor,
        payment_method=config.receipt_payment_method,
        payment_object=config.receipt_payment_object,
        tax=config.receipt_tax,
    )
    encoded_receipt = encode_receipt(payload)
    shp_params = collect_shp_params({"Shp_order_id": order_id})
    out_sum = format_amount_minor(amount_minor)

    signature_base = build_signature_base(
        config.merchant_login,
        out_sum,
        invoice_id,
        encoded_receipt,
        config.password1_for_mode(is_test),
        shp_params=shp_params,
    )

    fields: dict[str, str] = {
        "MerchantLogin": config.merchant_login,
        "OutSum": out_sum,
        "InvId": invoice_id,
        "Description": description[:100],
        "Receipt": encoded_receipt,
        "SignatureValue": hash_signature(signature_base, config.hash_algorithm),
        "Culture": "ru",
        "Encoding": "utf-8",
        "Shp_order_id": order_id,
    }
    if email:
        fields["Email"] = email
    if expiration_date:
        fields["ExpirationDate"] = expiration_date
    if is_test:
        # RU: Без IsTest=1 (или при IsTest=0) операция боевая и спишет реальные деньги.
        fields["IsTest"] = "1"

    return {"method": "POST", "action": ROBOKASSA_CHECKOUT_URL, "fields": fields}


# ---------------------------------------------------------------- ResultURL


@dataclass(frozen=True)
class ResultVerification:
    """Результат проверки ResultURL до каких-либо изменений в базе."""

    ok: bool
    reason: str
    order_id: str = ""
    invoice_id: str = ""
    out_sum: str = ""


def verify_result_callback(
    payload: dict[str, str],
    *,
    config: RobokassaConfig,
    expected_amount_minor: int | None,
    expected_invoice_id: str | None,
    is_test: bool,
) -> ResultVerification:
    """Проверить подпись и содержимое ResultURL.

    Функция намеренно ничего не меняет и ничего не знает про базу: ей передают
    ожидаемые значения, она возвращает вердикт. Это делает её тестируемой без
    БД и без сети — основной приём этого проекта.

    Тонкость, на которой ломаются интеграции: в боевом режиме ``OutSum``
    приходит с ШЕСТЬЮ знаками после запятой (``799.000000``), в тестовом — с
    двумя. Подпись считается по строке ровно в том виде, в каком она пришла,
    поэтому переформатировать её нельзя. Сверка с нашей суммой идёт отдельно,
    через ``Decimal``, которому число знаков безразлично.
    """
    out_sum = str(payload.get("OutSum") or "").strip()
    invoice_id = str(payload.get("InvId") or "").strip()
    signature_value = str(
        payload.get("SignatureValue") or payload.get("Signaturevalue") or payload.get("signaturevalue") or ""
    ).strip()
    shp_params = collect_shp_params(payload)
    order_id = str(dict(shp_params).get("Shp_order_id") or "").strip()

    if not out_sum or not invoice_id or not signature_value or not order_id:
        return ResultVerification(False, "missing_parameters")

    if expected_invoice_id is not None and str(expected_invoice_id) != invoice_id:
        return ResultVerification(False, "invoice_mismatch", order_id, invoice_id, out_sum)

    if expected_amount_minor is not None and not amount_matches_minor(out_sum, expected_amount_minor):
        return ResultVerification(False, "amount_mismatch", order_id, invoice_id, out_sum)

    password2 = config.password2_for_mode(is_test)
    if not password2:
        return ResultVerification(False, "password_missing", order_id, invoice_id, out_sum)

    expected_signature = hash_signature(
        build_signature_base(out_sum, invoice_id, password2, shp_params=shp_params),
        config.hash_algorithm,
    )
    # RU: Сравнение регистронезависимое — Robokassa шлёт hex в верхнем регистре.
    if not secrets.compare_digest(expected_signature.lower(), signature_value.lower()):
        return ResultVerification(False, "invalid_signature", order_id, invoice_id, out_sum)

    return ResultVerification(True, "ok", order_id, invoice_id, out_sum)


def verify_success_callback(
    payload: dict[str, str],
    *,
    config: RobokassaConfig,
    is_test: bool,
) -> ResultVerification:
    """Проверить подпись SuccessURL — она считается по Паролю #1, а не #2.

    Даже при верной подписи это НЕ подтверждение оплаты: страница успеха лишь
    показывает статус заказа из нашей базы, который выставляет только ResultURL.
    Проверка нужна, чтобы не рисовать «спасибо за оплату» по произвольной ссылке.
    """
    out_sum = str(payload.get("OutSum") or "").strip()
    invoice_id = str(payload.get("InvId") or "").strip()
    signature_value = str(payload.get("SignatureValue") or payload.get("Signaturevalue") or "").strip()
    shp_params = collect_shp_params(payload)
    order_id = str(dict(shp_params).get("Shp_order_id") or "").strip()

    if not out_sum or not invoice_id or not signature_value:
        return ResultVerification(False, "missing_parameters", order_id, invoice_id, out_sum)

    password1 = config.password1_for_mode(is_test)
    if not password1:
        return ResultVerification(False, "password_missing", order_id, invoice_id, out_sum)

    expected_signature = hash_signature(
        build_signature_base(out_sum, invoice_id, password1, shp_params=shp_params),
        config.hash_algorithm,
    )
    if not secrets.compare_digest(expected_signature.lower(), signature_value.lower()):
        return ResultVerification(False, "invalid_signature", order_id, invoice_id, out_sum)
    return ResultVerification(True, "ok", order_id, invoice_id, out_sum)


def build_opstate_request(config: RobokassaConfig, invoice_id: str) -> tuple[str, dict[str, str]]:
    """Параметры для сверки статуса платежа (``OpStateExt``).

    Нужна на случай, когда все повторы ResultURL не дошли — заказ повиснет в
    ``pending``, и его надо досверить фоновым скриптом. В тестовом режиме сервис
    не работает: тестовые платежи через него не видны.
    """
    signature = hash_signature(
        build_signature_base(config.merchant_login, invoice_id, config.password2),
        config.hash_algorithm,
    )
    return ROBOKASSA_OPSTATE_URL, {
        "MerchantLogin": config.merchant_login,
        "InvoiceID": invoice_id,
        "Signature": signature,
    }


# RU: State.Code из OpStateExt. 100 — деньги получены и операция завершена.
OPSTATE_SUCCESS_CODE = 100
OPSTATE_CODES = {
    5: "инициализирована",
    10: "отменена, деньги не получены",
    20: "приостановлена (HOLD)",
    50: "деньги получены, идёт зачисление",
    60: "отказ, деньги возвращены",
    80: "приостановлена",
    100: "успешно завершена",
}


def result_ok_response(invoice_id: str) -> str:
    """Robokassa считает уведомление принятым только при таком ответе.

    Ровно ``OK<InvId>``, text/plain, без переводов строки и разметки. Любой
    другой ответ — и Robokassa будет слать уведомление повторно, а затем
    напишет владельцу магазина письмо.
    """
    return f"OK{invoice_id}"
