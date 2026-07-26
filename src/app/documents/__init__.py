"""Шаблоны документов и правила их сборки."""

from .schema import (  # noqa: F401
    Condition,
    DocumentTemplate,
    Clause,
    evaluate_condition,
    evaluate_conditions,
    render_document,
    template_to_dict,
)
