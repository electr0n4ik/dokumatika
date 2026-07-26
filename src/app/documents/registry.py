"""Реестр документов.

Бесплатная политика и платный комплект собираются в одну таблицу, из которой
живут и сервер, и браузер. Клиент получает её одним JSON (``/api/wizard.json``),
поэтому добавление документа не требует править фронтенд.
"""

from __future__ import annotations

from typing import Any

from .paid import PAID_TEMPLATES
from .policy import POLICY_TEMPLATE
from .schema import DocumentTemplate

FREE_DOCUMENTS: tuple[DocumentTemplate, ...] = (POLICY_TEMPLATE,)
PAID_DOCUMENTS: tuple[DocumentTemplate, ...] = PAID_TEMPLATES
ALL_DOCUMENTS: tuple[DocumentTemplate, ...] = FREE_DOCUMENTS + PAID_DOCUMENTS

DOCUMENTS_BY_CODE: dict[str, DocumentTemplate] = {doc.code: doc for doc in ALL_DOCUMENTS}


def get_document(code: str) -> DocumentTemplate | None:
    return DOCUMENTS_BY_CODE.get(str(code or "").strip())


def documents_payload(*, include_paid: bool = True) -> dict[str, Any]:
    """Данные для браузера.

    Платные шаблоны отдаются всегда: их ценность в юридической проработке и в
    том, что они собираются под конкретные ответы, а не в секретности текста.
    Прятать их за оплатой на уровне API значило бы усложнить систему ради
    защиты от копирования, которое всё равно тривиально (документ у покупателя
    на руках). Доступ к сборке комплекта закрывает страница заказа.
    """
    documents = ALL_DOCUMENTS if include_paid else FREE_DOCUMENTS
    return {
        "free": [doc.code for doc in FREE_DOCUMENTS],
        "paid": [doc.code for doc in PAID_DOCUMENTS],
        "templates": [doc.to_dict() for doc in documents],
    }
