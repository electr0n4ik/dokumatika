"""Каталог продуктов.

Одно место, где живут цена, состав и человекочитаемые названия. HTTP-слой,
страницы, чек Robokassa и письмо берут данные отсюда — расхождение цены на
витрине и в чеке структурно невозможно.

Цена хранится в копейках (``amount_minor``) — деньги во float не считаем.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    code: str
    title: str
    short_title: str
    amount_minor: int
    summary: str
    # RU: Что физически получает покупатель. Идёт и на витрину, и в письмо.
    includes: tuple[str, ...]
    # RU: Коды документов из app.documents.registry, которые открывает покупка.
    document_codes: tuple[str, ...]

    @property
    def amount_rub(self) -> str:
        rub, kop = divmod(self.amount_minor, 100)
        return f"{rub}" if kop == 0 else f"{rub},{kop:02d}"

    @property
    def price_label(self) -> str:
        return f"{self.amount_rub} ₽"


KOMPLEKT_152FZ = Product(
    code="komplekt_152fz",
    title="Комплект документов по 152-ФЗ",
    short_title="Комплект 152-ФЗ",
    amount_minor=79900,
    summary=(
        "Всё, что закон требует от оператора персональных данных, кроме самой "
        "политики — она бесплатна. Документы заполняются вашими ответами и "
        "скачиваются одним архивом."
    ),
    includes=(
        "Согласие на обработку персональных данных",
        "Отдельное согласие на рекламную рассылку",
        "Политика в отношении файлов cookie",
        "Приказ о назначении ответственного за организацию обработки ПД",
        "Форма отзыва согласия субъектом персональных данных",
        "Журнал учёта обращений субъектов персональных данных",
        "Памятка по подаче уведомления в Роскомнадзор",
    ),
    document_codes=(
        "consent",
        "consent_marketing",
        "cookie_policy",
        "order_responsible",
        "consent_withdrawal",
        "requests_journal",
        "rkn_notice_guide",
    ),
)

PRODUCTS: dict[str, Product] = {KOMPLEKT_152FZ.code: KOMPLEKT_152FZ}

DEFAULT_PRODUCT_CODE = KOMPLEKT_152FZ.code


def get_product(code: str | None) -> Product | None:
    return PRODUCTS.get(str(code or "").strip())
