"""Тесты движка документов и связности шаблонов с визардом.

Главная опасность продукта — тихая рассинхронизация: в шаблоне появился
плейсхолдер, которого никто не вычисляет, или условие по полю, которого нет в
визарде. Пользователь получит документ с дырой и не поймёт, почему. Поэтому
связность проверяется автоматически по всем шаблонам сразу.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.documents.registry import ALL_DOCUMENTS, FREE_DOCUMENTS, PAID_DOCUMENTS, documents_payload
from app.documents.schema import (
    Clause,
    Condition,
    DocumentTemplate,
    PLACEHOLDER_RE,
    evaluate_condition,
    evaluate_conditions,
    fill_placeholders,
    render_document,
)
from app.documents.wizard import QUESTIONS, VALUE_RULES, compute_values, wizard_payload
from app.products import KOMPLEKT_152FZ

ANSWER_FIELDS = {question.id for question in QUESTIONS}
VALUE_KEYS = {rule.key for rule in VALUE_RULES}


class TestConditions:
    @pytest.mark.parametrize(
        ("op", "value", "actual", "expected"),
        [
            ("truthy", None, True, True),
            ("truthy", None, "", False),
            ("falsy", None, False, True),
            ("eq", "shop", "shop", True),
            ("eq", "shop", "site", False),
            ("ne", "shop", "site", True),
            ("in", ["ip", "ooo"], "ip", True),
            ("in", ["ip", "ooo"], "individual", False),
            ("not_in", ["ip"], "ooo", True),
            ("contains", "email", ["name", "email"], True),
            ("contains", "email", ["name"], False),
            ("not_contains", "email", ["name"], True),
        ],
    )
    def test_operations(self, op: str, value: object, actual: object, expected: bool) -> None:
        condition = Condition(field="x", op=op, value=value)
        assert evaluate_condition(condition, {"x": actual}) is expected

    def test_unknown_operation_is_false(self) -> None:
        """Неизвестная операция не должна «на всякий случай» включать пункт."""
        assert evaluate_condition(Condition(field="x", op="regex", value=".*"), {"x": "y"}) is False

    def test_missing_field_is_falsy(self) -> None:
        assert evaluate_condition(Condition(field="absent"), {}) is False

    def test_empty_conditions_always_include(self) -> None:
        assert evaluate_conditions((), {}) is True

    def test_conditions_combine_with_and(self) -> None:
        conditions = (Condition("a"), Condition("b"))
        assert evaluate_conditions(conditions, {"a": True, "b": True})
        assert not evaluate_conditions(conditions, {"a": True, "b": False})


class TestPlaceholders:
    def test_substitutes_known_values(self) -> None:
        assert fill_placeholders("ИНН {{inn}}", {"inn": "770123456789"}) == "ИНН 770123456789"

    def test_missing_value_is_visible(self) -> None:
        """Тихая пустота в юридическом документе опаснее заметной метки."""
        assert fill_placeholders("ИНН {{inn}}", {}) == "ИНН [не заполнено: inn]"

    def test_empty_string_is_treated_as_missing(self) -> None:
        assert "[не заполнено" in fill_placeholders("{{inn}}", {"inn": ""})

    def test_list_value_joined(self) -> None:
        assert fill_placeholders("{{x}}", {"x": ["а", "б"]}) == "а, б"


class TestRendering:
    def _template(self) -> DocumentTemplate:
        return DocumentTemplate(
            code="demo",
            title="Документ {{operator_name}}",
            filename="demo",
            clauses=(
                Clause(id="always", title="Общее", paragraphs=("Оператор: {{operator_full}}.",)),
                Clause(
                    id="shop_only",
                    title="Оплата",
                    paragraphs=("Только для магазина.",),
                    when=(Condition("resource", "eq", "shop"),),
                ),
            ),
        )

    def test_conditional_clause_excluded(self) -> None:
        rendered = render_document(self._template(), {"resource": "site"}, {"operator_name": "Иванов"})
        assert [clause.id for clause in rendered.clauses] == ["always"]

    def test_conditional_clause_included(self) -> None:
        rendered = render_document(self._template(), {"resource": "shop"}, {"operator_name": "Иванов"})
        assert [clause.id for clause in rendered.clauses] == ["always", "shop_only"]

    def test_title_placeholders_filled(self) -> None:
        rendered = render_document(self._template(), {}, {"operator_name": "Иванов"})
        assert rendered.title == "Документ Иванов"

    def test_plain_text_numbers_clauses(self) -> None:
        rendered = render_document(self._template(), {"resource": "shop"}, {"operator_full": "ИП Иванов"})
        text = rendered.plain_text
        assert "1. Общее" in text and "2. Оплата" in text


class TestWizardValues:
    def _answers(self) -> dict:
        return {
            "resource": "shop",
            "operator_type": "ip",
            "operator_name": "Иванов Иван Иванович",
            "inn": "770123456789",
            "data_types": ["name", "email", "phone"],
            "purposes": ["feedback", "order"],
            "third_parties": ["hosting", "none"],
            "site_url": "https://example.ru",
            "contact_email": "privacy@example.ru",
        }

    def test_operator_full_joins_form_and_name(self) -> None:
        values = compute_values(self._answers())
        assert values["operator_full"] == "Индивидуальный предприниматель Иванов Иван Иванович"

    def test_operator_full_without_form_for_company(self) -> None:
        answers = self._answers() | {"operator_type": "ooo", "operator_name": "ООО «Ромашка»"}
        assert compute_values(answers)["operator_full"] == "ООО «Ромашка»"

    def test_labels_use_human_names(self) -> None:
        values = compute_values(self._answers())
        assert values["data_list"] == "Фамилия, имя, отчество, Адрес электронной почты, Номер телефона"

    def test_none_option_excluded_from_list(self) -> None:
        """«Никому не передаю» не должно попасть в перечень третьих лиц."""
        assert "Никому" not in compute_values(self._answers())["third_parties_list"]

    def test_fallback_for_empty_field(self) -> None:
        assert compute_values(self._answers())["ogrn"] == "—"

    def test_responsible_person_is_never_invented(self) -> None:
        """Пустое поле обязано остаться пустым.

        Подстановка обобщённого «руководитель оператора» вписывала бы в приказ и
        в уведомление для Роскомнадзора лицо, которого не существует.
        """
        assert compute_values(self._answers())["responsible_person"] == ""

    def test_responsible_person_is_required(self) -> None:
        question = next(q for q in QUESTIONS if q.id == "responsible_person")
        assert question.required, "ответственного требует ч. 1 ст. 18.1 152-ФЗ"

    def test_doc_date_defaults_to_today(self) -> None:
        values = compute_values(self._answers(), today=date(2026, 7, 26))
        assert values["doc_date"] == "26.07.2026"

    def test_doc_date_converted_from_iso(self) -> None:
        answers = self._answers() | {"doc_date": "2026-01-09"}
        assert compute_values(answers)["doc_date"] == "09.01.2026"


class TestTemplateIntegrity:
    """Связность всех шаблонов с визардом — самый ценный тест файла."""

    def test_catalog_matches_registry(self) -> None:
        codes = {document.code for document in PAID_DOCUMENTS}
        assert set(KOMPLEKT_152FZ.document_codes) == codes

    def test_includes_count_matches_documents(self) -> None:
        assert len(KOMPLEKT_152FZ.includes) == len(KOMPLEKT_152FZ.document_codes)

    def test_free_and_paid_do_not_overlap(self) -> None:
        free = {document.code for document in FREE_DOCUMENTS}
        paid = {document.code for document in PAID_DOCUMENTS}
        assert not (free & paid)

    def test_codes_are_unique(self) -> None:
        codes = [document.code for document in ALL_DOCUMENTS]
        assert len(codes) == len(set(codes))

    def test_paid_flag_consistent(self) -> None:
        assert all(not document.paid for document in FREE_DOCUMENTS)
        assert all(document.paid for document in PAID_DOCUMENTS)

    def test_every_placeholder_is_computed(self) -> None:
        missing: set[str] = set()
        for document in ALL_DOCUMENTS:
            texts = [document.title, document.subtitle]
            for clause in document.clauses:
                texts.append(clause.title)
                texts.extend(clause.paragraphs)
                for row in clause.rows:
                    texts.extend(row)
            for text in texts:
                missing |= {key for key in PLACEHOLDER_RE.findall(text or "") if key not in VALUE_KEYS}
        assert not missing, f"плейсхолдеры без правила вычисления: {sorted(missing)}"

    def test_every_condition_field_exists_in_wizard(self) -> None:
        unknown: set[str] = set()
        for document in ALL_DOCUMENTS:
            for clause in document.clauses:
                unknown |= {c.field for c in clause.when if c.field not in ANSWER_FIELDS}
        assert not unknown, f"условия по несуществующим полям: {sorted(unknown)}"

    def test_condition_operations_are_supported(self) -> None:
        from app.documents.schema import OPERATIONS

        for document in ALL_DOCUMENTS:
            for clause in document.clauses:
                for condition in clause.when:
                    assert condition.op in OPERATIONS, f"{document.code}: {condition.op}"

    def test_clause_ids_unique_within_document(self) -> None:
        for document in ALL_DOCUMENTS:
            ids = [clause.id for clause in document.clauses]
            assert len(ids) == len(set(ids)), f"дубли id в {document.code}"

    def test_documents_are_substantial(self) -> None:
        """Защита от заглушек: документ обязан быть документом, а не парой строк."""
        for document in ALL_DOCUMENTS:
            assert len(document.clauses) >= 4, f"{document.code}: слишком мало пунктов"
            assert document.filename and document.title

    def test_all_documents_render_with_full_answers(self) -> None:
        answers = {
            "resource": "shop",
            "has_forms": True,
            "operator_type": "ip",
            "operator_name": "Иванов Иван Иванович",
            "inn": "770123456789",
            "ogrn": "304770000000001",
            "data_types": ["name", "email", "phone", "cookies", "payment", "address"],
            "purposes": ["feedback", "contract", "order", "analytics", "marketing"],
            "third_parties": ["hosting", "payment_service", "delivery_service"],
            "cross_border": True,
            "site_url": "https://example.ru",
            "contact_email": "privacy@example.ru",
            "city": "Москва",
            "responsible_person": "Иванов И.И.",
        }
        values = compute_values(answers)
        for document in ALL_DOCUMENTS:
            rendered = render_document(document, answers, values)
            assert rendered.clauses, f"{document.code} отрендерился пустым"
            assert "[не заполнено" not in rendered.plain_text, f"{document.code}: дыра при полных ответах"

    def test_all_documents_render_with_minimal_answers(self) -> None:
        """Даже при пустых ответах рендер не должен падать."""
        for document in ALL_DOCUMENTS:
            render_document(document, {}, compute_values({}))


class TestPayloads:
    def test_wizard_payload_is_serializable(self) -> None:
        import json

        payload = wizard_payload()
        json.dumps(payload, ensure_ascii=False)
        assert len(payload["steps"]) == 5
        assert payload["questions"] and payload["valueRules"]

    def test_documents_payload_lists_codes(self) -> None:
        payload = documents_payload()
        assert payload["free"] == ["policy"]
        assert len(payload["paid"]) == 7
        assert len(payload["templates"]) == len(ALL_DOCUMENTS)

    def test_free_only_payload_excludes_paid(self) -> None:
        payload = documents_payload(include_paid=False)
        assert len(payload["templates"]) == len(FREE_DOCUMENTS)

    def test_every_question_id_unique(self) -> None:
        ids = [question.id for question in QUESTIONS]
        assert len(ids) == len(set(ids))

    def test_value_rules_reference_existing_fields(self) -> None:
        for rule in VALUE_RULES:
            if rule.type in {"field", "map", "labels"}:
                assert rule.field in ANSWER_FIELDS, f"правило {rule.key} ссылается на {rule.field}"

    def test_join_rules_reference_existing_keys(self) -> None:
        for rule in VALUE_RULES:
            if rule.type == "join":
                assert all(part in VALUE_KEYS for part in rule.parts)
