"""Формат шаблона документа и движок его сборки.

Ключевое архитектурное решение проекта: **шаблоны описаны данными, а не кодом**.

Один и тот же шаблон собирается дважды —
* на сервере (Python) — для предпросмотра в выдаче, тестов и проверок;
* в браузере (JS) — для реальной генерации документа пользователя.

Чтобы результат совпадал, язык условий сделан нарочито примитивным: никакого
парсинга выражений, только структура ``{field, op, value}``. Реализовать её
одинаково на двух языках — двадцать строк, ошибиться негде. Любая попытка
завести полноценный DSL немедленно родила бы расхождение между Python и JS.

Плейсхолдеры вида ``{{operator_name}}`` подставляются из плоского словаря
значений. Неизвестный плейсхолдер не молчит: он превращается в видимую метку
``[не заполнено: operator_name]``, потому что тихая дыра в юридическом
документе хуже заметной.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER_RE = re.compile(r"\{\{([a-z0-9_]+)\}\}")

# RU: Операции условий. Список закрыт — в JS реализованы ровно эти.
OPERATIONS = frozenset({"truthy", "falsy", "eq", "ne", "in", "not_in", "contains", "not_contains"})


@dataclass(frozen=True)
class Condition:
    """Одно условие включения пункта.

    ``field``    — ключ в ответах визарда.
    ``op``       — операция из ``OPERATIONS``.
    ``value``    — значение для сравнения (для ``truthy``/``falsy`` не нужно).
    """

    field: str
    op: str = "truthy"
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"field": self.field, "op": self.op}
        if self.value is not None:
            payload["value"] = self.value
        return payload


def evaluate_condition(condition: Condition, answers: dict[str, Any]) -> bool:
    """Вычислить одно условие. Незнакомая операция = условие не выполнено."""
    actual = answers.get(condition.field)
    op = condition.op

    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    if op == "eq":
        return actual == condition.value
    if op == "ne":
        return actual != condition.value
    if op == "in":
        return actual in (condition.value or [])
    if op == "not_in":
        return actual not in (condition.value or [])
    if op == "contains":
        return isinstance(actual, (list, tuple, set, str)) and condition.value in actual
    if op == "not_contains":
        return not (isinstance(actual, (list, tuple, set, str)) and condition.value in actual)
    return False


def evaluate_conditions(conditions: tuple[Condition, ...], answers: dict[str, Any]) -> bool:
    """Все условия пункта соединяются логическим И. Пустой список = включать всегда."""
    return all(evaluate_condition(condition, answers) for condition in conditions)


@dataclass(frozen=True)
class Clause:
    """Пункт документа: заголовок + абзацы, возможно условный.

    ``kind`` управляет версткой: ``text`` — абзацы, ``list`` — маркированный
    список, ``ordered`` — нумерованный, ``table`` — таблица (в ``rows``).
    """

    id: str
    title: str = ""
    paragraphs: tuple[str, ...] = ()
    kind: str = "text"
    rows: tuple[tuple[str, ...], ...] = ()
    when: tuple[Condition, ...] = ()
    # RU: Пункт, который нельзя выкинуть — требование закона, а не удобство.
    required_by_law: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.title:
            payload["title"] = self.title
        if self.paragraphs:
            payload["paragraphs"] = list(self.paragraphs)
        if self.rows:
            payload["rows"] = [list(row) for row in self.rows]
        if self.when:
            payload["when"] = [condition.to_dict() for condition in self.when]
        if self.required_by_law:
            payload["requiredByLaw"] = True
        return payload


@dataclass(frozen=True)
class DocumentTemplate:
    """Готовый к сборке документ."""

    code: str
    title: str
    filename: str
    # RU: Подзаголовок под H1 внутри самого документа (например, «по 152-ФЗ»).
    subtitle: str = ""
    clauses: tuple[Clause, ...] = ()
    # RU: Платный документ доступен только после оплаты; бесплатный — всем.
    paid: bool = False
    # RU: Короткое пояснение «зачем этот документ» — для витрины и письма.
    purpose: str = ""
    # RU: Пометка о правовом основании — выводится мелким шрифтом в документе.
    legal_basis: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "filename": self.filename,
            "subtitle": self.subtitle,
            "paid": self.paid,
            "purpose": self.purpose,
            "legalBasis": self.legal_basis,
            "notes": list(self.notes),
            "clauses": [clause.to_dict() for clause in self.clauses],
        }


def template_to_dict(template: DocumentTemplate) -> dict[str, Any]:
    return template.to_dict()


def fill_placeholders(text: str, values: dict[str, Any]) -> str:
    """Подставить значения. Отсутствующие — видимой меткой, а не пустотой."""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        if value is None or value == "":
            return f"[не заполнено: {key}]"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return str(value)

    return PLACEHOLDER_RE.sub(substitute, str(text or ""))


@dataclass(frozen=True)
class RenderedClause:
    id: str
    title: str
    paragraphs: tuple[str, ...]
    kind: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class RenderedDocument:
    code: str
    title: str
    subtitle: str
    filename: str
    legal_basis: str
    clauses: tuple[RenderedClause, ...]

    @property
    def plain_text(self) -> str:
        """Текстовая версия — для писем, тестов и кнопки «скопировать»."""
        lines: list[str] = [self.title]
        if self.subtitle:
            lines.append(self.subtitle)
        lines.append("")
        for index, clause in enumerate(self.clauses, start=1):
            if clause.title:
                lines.append(f"{index}. {clause.title}")
            lines.extend(clause.paragraphs)
            for row in clause.rows:
                lines.append(" | ".join(row))
            lines.append("")
        return "\n".join(lines).strip()


def render_document(
    template: DocumentTemplate,
    answers: dict[str, Any],
    values: dict[str, Any],
) -> RenderedDocument:
    """Собрать документ: отфильтровать пункты по условиям и подставить значения."""
    rendered: list[RenderedClause] = []
    for clause in template.clauses:
        if not evaluate_conditions(clause.when, answers):
            continue
        rendered.append(
            RenderedClause(
                id=clause.id,
                title=fill_placeholders(clause.title, values),
                paragraphs=tuple(fill_placeholders(item, values) for item in clause.paragraphs),
                kind=clause.kind,
                rows=tuple(
                    tuple(fill_placeholders(cell, values) for cell in row) for row in clause.rows
                ),
            )
        )
    return RenderedDocument(
        code=template.code,
        title=fill_placeholders(template.title, values),
        subtitle=fill_placeholders(template.subtitle, values),
        filename=template.filename,
        legal_basis=template.legal_basis,
        clauses=tuple(rendered),
    )
