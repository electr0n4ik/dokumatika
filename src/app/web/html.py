"""Микро-хелперы для сборки HTML.

Шаблонизатора в проекте нет: страниц немного, а лишняя зависимость на сервере,
где живут пять проектов, стоит дороже удобства. Взамен — жёсткое правило:

**весь текст от пользователя и из конфига проходит через ``esc``.**

Сырой HTML допускается только там, где он написан нами и помечен явно —
для этого есть тип ``Raw``. Если значение не ``Raw``, оно экранируется. Так
XSS невозможен по построению, а не по внимательности.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape


class Raw(str):
    """Строка, которую уже можно вставлять в HTML как есть.

    Оборачивать в ``Raw`` можно только литералы и результат функций этого
    модуля — никогда пользовательский ввод.
    """

    __slots__ = ()


def esc(value: object) -> str:
    """Экранировать значение для вставки в текст или атрибут."""
    if isinstance(value, Raw):
        return str(value)
    return escape(str(value if value is not None else ""), quote=True)


def attrs(**pairs: object) -> str:
    """Собрать строку атрибутов.

    ``None`` и ``False`` опускают атрибут целиком, ``True`` даёт булев атрибут.
    Подчёркивания в имени превращаются в дефисы (``data_role`` -> ``data-role``),
    а хвостовое подчёркивание срезается (``for_`` -> ``for``).
    """
    parts: list[str] = []
    for raw_name, value in pairs.items():
        if value is None or value is False:
            continue
        name = raw_name.rstrip("_").replace("_", "-")
        if value is True:
            parts.append(name)
        else:
            parts.append(f'{name}="{esc(value)}"')
    return (" " + " ".join(parts)) if parts else ""


def tag(name: str, content: object = "", **pairs: object) -> Raw:
    return Raw(f"<{name}{attrs(**pairs)}>{esc(content)}</{name}>")


def void(name: str, **pairs: object) -> Raw:
    return Raw(f"<{name}{attrs(**pairs)}>")


def join(parts: Iterable[object], separator: str = "") -> Raw:
    """Склеить УЖЕ ГОТОВЫЕ куски разметки.

    Части не экранируются: сюда приходят фрагменты, собранные вызывающим кодом,
    внутри которых значения уже пропущены через ``esc``. Типичный вызов::

        join([f'<li>{esc(item)}</li>' for item in items])

    Экранировать здесь ещё раз нельзя — теги превратятся в видимый текст. Зато
    и передавать в ``join`` сырые пользовательские данные нельзя: сначала
    ``esc``, потом сборка фрагмента, потом ``join``.

    За соблюдением этого правила следит ``test_no_escaped_markup_in_pages``:
    он ищет в готовых страницах следы двойного экранирования.
    """
    return Raw(separator.join(str(part) for part in parts))


def classes(*names: object) -> str:
    """Собрать class из кусочков, отбрасывая пустые и ложные."""
    return " ".join(str(name) for name in names if name)


def strip_tags(value: str) -> str:
    """Убрать разметку — нужно для title и JSON-LD, где теги недопустимы."""
    result: list[str] = []
    inside = False
    for char in str(value or ""):
        if char == "<":
            inside = True
        elif char == ">":
            inside = False
        elif not inside:
            result.append(char)
    return "".join(result)
