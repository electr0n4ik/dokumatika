"""Структурные логи одной строкой JSON — их удобно грепать в journald.

Правило проекта: в лог НИКОГДА не попадают персональные данные пользователя и
секреты. Email маскируется (``ma***@example.com``), подписи и пароли не логируются
вообще.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password1",
        "password2",
        "signature",
        "signaturevalue",
        "admin_token",
        "token",
        "access_token",
    }
)


def mask_email(value: str) -> str:
    """``ivan.petrov@mail.ru`` -> ``iv***@mail.ru``. Пустое остаётся пустым."""
    text = str(value or "").strip()
    if "@" not in text:
        return "***" if text else ""
    local, _, domain = text.partition("@")
    visible = local[:2]
    return f"{visible}***@{domain}"


def _sanitize(key: str, value: Any) -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        return "***"
    if key.lower().endswith("email"):
        return mask_email(str(value))
    return value


def log_event(event: str, **fields: Any) -> None:
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
    }
    for key, value in fields.items():
        payload[key] = _sanitize(key, value)
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        line = json.dumps({"ts": payload["ts"], "event": event, "log_error": "unserializable"})
    print(line, file=sys.stdout, flush=True)
