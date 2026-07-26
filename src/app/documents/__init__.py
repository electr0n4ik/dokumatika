"""Шаблоны документов и правила их сборки."""

from .schema import (  # noqa: F401
    Clause,
    Condition,
    DocumentTemplate,
    evaluate_condition,
    evaluate_conditions,
    render_document,
    template_to_dict,
)
