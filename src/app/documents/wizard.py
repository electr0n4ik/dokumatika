"""Вопросы визарда и правила вычисления значений для подстановки.

Пять шагов — ровно те, что нужны и для политики, и для остальных документов, и
для полей уведомления в Роскомнадзор. Лишних вопросов нет: каждый ответ
куда-то подставляется, иначе его здесь быть не должно.

Значения плейсхолдеров описаны **декларативно** (``ValueRule``), а не кодом.
Причина та же, что и у условий в ``schema.py``: правила исполняются дважды —
на сервере (Python) и в браузере (JS). Декларативную таблицу из пяти типов
правил невозможно реализовать по-разному; произвольный код — легко.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

# --------------------------------------------------------------------- вопросы


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    hint: str = ""


@dataclass(frozen=True)
class Question:
    """Один вопрос визарда.

    ``kind``: ``radio`` — один вариант, ``checkbox`` — несколько, ``text`` —
    строка, ``date`` — дата, ``bool`` — да/нет.
    """

    id: str
    step: int
    title: str
    kind: str
    options: tuple[Option, ...] = ()
    hint: str = ""
    placeholder: str = ""
    required: bool = True
    # RU: Показывать вопрос только если выполнены условия (формат как в schema.Condition).
    when: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "step": self.step,
            "title": self.title,
            "kind": self.kind,
            "required": self.required,
        }
        if self.options:
            payload["options"] = [
                {
                    "value": option.value,
                    "label": option.label,
                    **({"hint": option.hint} if option.hint else {}),
                }
                for option in self.options
            ]
        if self.hint:
            payload["hint"] = self.hint
        if self.placeholder:
            payload["placeholder"] = self.placeholder
        if self.when:
            payload["when"] = list(self.when)
        return payload


STEPS: tuple[tuple[int, str, str], ...] = (
    (1, "Ресурс", "Для чего нужна политика"),
    (2, "Оператор", "Кто обрабатывает данные"),
    (3, "Данные", "Что вы собираете"),
    (4, "Цели", "Зачем и кому передаёте"),
    (5, "Реквизиты", "Как вас указать в документе"),
)


QUESTIONS: tuple[Question, ...] = (
    # ---------------------------------------------------------------- шаг 1
    Question(
        id="resource",
        step=1,
        title="Где вы собираете персональные данные?",
        kind="radio",
        hint="От этого зависит, какие разделы войдут в документ.",
        options=(
            Option("site", "Сайт или лендинг", "Формы заявки, обратная связь, подписка"),
            Option("shop", "Интернет-магазин", "Добавим оплату, доставку и данные заказа"),
            Option("app", "Мобильное приложение", "Добавим разрешения устройства и идентификаторы"),
            Option("bot", "Телеграм-бот", "Учтём специфику мессенджера"),
        ),
    ),
    Question(
        id="has_forms",
        step=1,
        title="На ресурсе есть формы, куда посетитель вводит свои данные?",
        kind="bool",
        hint=(
            "Заявка, обратный звонок, подписка, регистрация, оформление заказа. "
            "Если форм нет совсем и вы не собираете cookie — политика может быть не нужна."
        ),
    ),
    # ---------------------------------------------------------------- шаг 2
    Question(
        id="operator_type",
        step=2,
        title="Кто выступает оператором персональных данных?",
        kind="radio",
        hint="Оператор — тот, кто решает, зачем и какие данные собирать. Это вы.",
        options=(
            Option("ip", "Индивидуальный предприниматель"),
            Option("ooo", "Организация (ООО, АО и др.)"),
            Option("self_employed", "Самозанятый (НПД)"),
            Option("individual", "Физическое лицо"),
        ),
    ),
    # ---------------------------------------------------------------- шаг 3
    Question(
        id="data_types",
        step=3,
        title="Какие данные вы собираете?",
        kind="checkbox",
        hint="Отмечайте только то, что действительно собираете — лишнее в документе вредит.",
        options=(
            Option("name", "Фамилия, имя, отчество"),
            Option("email", "Адрес электронной почты"),
            Option("phone", "Номер телефона"),
            Option("cookies", "Cookie и данные веб-аналитики", "Яндекс Метрика и подобные счётчики"),
            Option("payment", "Платёжные данные", "Сведения о заказах и оплатах"),
            Option("address", "Адрес доставки"),
            Option("social", "Аккаунты в социальных сетях и мессенджерах"),
            Option("birthdate", "Дата рождения"),
            Option("photo", "Фотография"),
            Option("passport", "Паспортные данные", "Собирайте только если это правда необходимо"),
        ),
    ),
    # ---------------------------------------------------------------- шаг 4
    Question(
        id="purposes",
        step=4,
        title="Зачем вы обрабатываете эти данные?",
        kind="checkbox",
        options=(
            Option("feedback", "Обратная связь и ответы на обращения"),
            Option("contract", "Заключение и исполнение договора"),
            Option("order", "Оформление, оплата и доставка заказов"),
            Option("analytics", "Статистика посещений и улучшение сайта"),
            Option("marketing", "Рекламные и информационные рассылки", "Потребует отдельного согласия"),
            Option("support", "Поддержка пользователей"),
        ),
    ),
    Question(
        id="third_parties",
        step=4,
        title="Кому вы передаёте данные?",
        kind="checkbox",
        required=False,
        hint="Сервисы, которыми вы пользуетесь, — это тоже передача данных.",
        options=(
            Option("hosting", "Хостинг-провайдер"),
            Option("analytics_service", "Сервис веб-аналитики"),
            Option("payment_service", "Платёжный сервис"),
            Option("delivery_service", "Служба доставки"),
            Option("crm", "CRM или сервис рассылок"),
            Option("none", "Никому не передаю"),
        ),
    ),
    Question(
        id="cross_border",
        step=4,
        title="Передаёте ли вы данные за пределы России?",
        kind="bool",
        required=False,
        hint=(
            "Зарубежный хостинг, аналитика или почтовый сервис — это трансграничная "
            "передача. Она требует отдельного уведомления в Роскомнадзор до начала передачи."
        ),
    ),
    # ---------------------------------------------------------------- шаг 5
    Question(
        id="operator_name",
        step=5,
        title="Наименование или ФИО оператора",
        kind="text",
        placeholder="Иванов Иван Иванович / ООО «Ромашка»",
    ),
    Question(
        id="inn",
        step=5,
        title="ИНН",
        kind="text",
        placeholder="770123456789",
    ),
    Question(
        id="ogrn",
        step=5,
        title="ОГРН / ОГРНИП",
        kind="text",
        required=False,
        placeholder="1027700123456",
        when=({"field": "operator_type", "op": "in", "value": ["ip", "ooo"]},),
    ),
    Question(
        id="site_url",
        step=5,
        title="Адрес сайта",
        kind="text",
        placeholder="https://example.ru",
    ),
    Question(
        id="contact_email",
        step=5,
        title="Email для обращений субъектов персональных данных",
        kind="text",
        hint="На этот адрес будут приходить запросы об удалении и отзыве согласия.",
        placeholder="privacy@example.ru",
    ),
    Question(
        id="city",
        step=5,
        title="Город",
        kind="text",
        required=False,
        placeholder="Москва",
    ),
    Question(
        id="responsible_person",
        step=5,
        title="Ответственный за организацию обработки персональных данных",
        kind="text",
        # RU: Обязателен намеренно. Ответственного требует ч. 1 ст. 18.1 152-ФЗ,
        # его ФИО идёт в приказ и в уведомление в Роскомнадзор. Подставлять сюда
        # обобщённое «руководитель оператора» нельзя: в документе появилось бы
        # несуществующее лицо, а в форме РКН такое значение просто не примут.
        hint="Требование ч. 1 ст. 18.1 152-ФЗ. Для ИП и самозанятого — обычно вы сами.",
        placeholder="Иванов Иван Иванович",
    ),
    Question(
        id="doc_date",
        step=5,
        title="Дата утверждения документа",
        kind="date",
        required=False,
        hint="Если не заполнить — подставим сегодняшнюю дату.",
    ),
)


def questions_for_step(step: int) -> tuple[Question, ...]:
    return tuple(question for question in QUESTIONS if question.step == step)


# ------------------------------------------------------------------- значения


@dataclass(frozen=True)
class ValueRule:
    """Правило вычисления одного плейсхолдера.

    ``type``:
    * ``field``  — взять ответ как есть;
    * ``labels`` — превратить выбранные значения в человеческий список по
      подписям вариантов;
    * ``map``    — подставить строку по значению ответа;
    * ``const``  — постоянный текст;
    * ``today``  — сегодняшняя дата в формате ДД.ММ.ГГГГ;
    * ``join``   — склеить несколько плейсхолдеров через пробел, пропуская пустые.
    """

    key: str
    type: str
    field: str = ""
    separator: str = ", "
    mapping: dict[str, str] | None = None
    text: str = ""
    parts: tuple[str, ...] = ()
    fallback: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"key": self.key, "type": self.type}
        for name, value in (
            ("field", self.field),
            ("text", self.text),
            ("fallback", self.fallback),
        ):
            if value:
                payload[name] = value
        if self.type == "labels":
            payload["separator"] = self.separator
        if self.mapping:
            payload["mapping"] = self.mapping
        if self.parts:
            payload["parts"] = list(self.parts)
        return payload


OPERATOR_FORM_LABELS = {
    "ip": "Индивидуальный предприниматель",
    "ooo": "",
    "self_employed": "Самозанятый",
    "individual": "",
}

RESOURCE_LABELS = {
    "site": "сайта",
    "shop": "интернет-магазина",
    "app": "мобильного приложения",
    "bot": "телеграм-бота",
}

VALUE_RULES: tuple[ValueRule, ...] = (
    ValueRule(key="operator_name", type="field", field="operator_name"),
    ValueRule(key="operator_form", type="map", field="operator_type", mapping=OPERATOR_FORM_LABELS),
    ValueRule(key="operator_full", type="join", parts=("operator_form", "operator_name")),
    ValueRule(key="inn", type="field", field="inn"),
    ValueRule(key="ogrn", type="field", field="ogrn", fallback="—"),
    ValueRule(key="site_url", type="field", field="site_url"),
    ValueRule(key="contact_email", type="field", field="contact_email"),
    ValueRule(key="city", type="field", field="city", fallback="—"),
    # RU: Без fallback — незаполненное поле должно быть видно как пропуск,
    # а не подменяться правдоподобной формулировкой.
    ValueRule(key="responsible_person", type="field", field="responsible_person"),
    ValueRule(key="resource_label", type="map", field="resource", mapping=RESOURCE_LABELS),
    ValueRule(key="data_list", type="labels", field="data_types"),
    ValueRule(key="purposes_list", type="labels", field="purposes"),
    ValueRule(key="third_parties_list", type="labels", field="third_parties", separator=", "),
    ValueRule(key="doc_date", type="field", field="doc_date", fallback=""),
)


def _option_labels(question_id: str) -> dict[str, str]:
    for question in QUESTIONS:
        if question.id == question_id:
            return {option.value: option.label for option in question.options}
    return {}


def _format_date(value: str) -> str:
    """ISO ``2026-07-26`` -> ``26.07.2026``. Другое возвращаем как есть."""
    parts = str(value or "").split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        year, month, day = parts
        return f"{day.zfill(2)}.{month.zfill(2)}.{year}"
    return str(value or "")


def compute_values(answers: dict[str, Any], *, today: date | None = None) -> dict[str, str]:
    """Вычислить все плейсхолдеры по ответам визарда."""
    current = today or date.today()
    values: dict[str, str] = {}

    for rule in VALUE_RULES:
        if rule.type == "field":
            # RU: str(None) даёт "None" — непустую строку, которая проскочила бы
            # и fallback, и подстановку даты, и напечаталась бы в шапке документа.
            raw = str(answers.get(rule.field) or "")
            text = _format_date(raw) if rule.field == "doc_date" else raw
            values[rule.key] = text.strip() or rule.fallback
        elif rule.type == "map":
            mapping = rule.mapping or {}
            values[rule.key] = mapping.get(str(answers.get(rule.field) or ""), rule.fallback)
        elif rule.type == "labels":
            labels = _option_labels(rule.field)
            selected = answers.get(rule.field) or []
            if isinstance(selected, str):
                selected = [selected]
            names = [labels.get(str(item), str(item)) for item in selected if str(item) != "none"]
            values[rule.key] = rule.separator.join(names) if names else rule.fallback
        elif rule.type == "const":
            values[rule.key] = rule.text
        elif rule.type == "today":
            values[rule.key] = current.strftime("%d.%m.%Y")
        elif rule.type == "join":
            chunks = [values.get(part, "").strip() for part in rule.parts]
            values[rule.key] = " ".join(chunk for chunk in chunks if chunk) or rule.fallback

    # RU: Дата документа по умолчанию — сегодня, иначе в шапке зияет пустота.
    if not values.get("doc_date"):
        values["doc_date"] = current.strftime("%d.%m.%Y")
    return values


def wizard_payload() -> dict[str, Any]:
    """Всё, что нужно браузеру для отрисовки визарда и сборки документов."""
    return {
        "steps": [{"index": index, "title": title, "subtitle": subtitle} for index, title, subtitle in STEPS],
        "questions": [question.to_dict() for question in QUESTIONS],
        "valueRules": [rule.to_dict() for rule in VALUE_RULES],
    }
