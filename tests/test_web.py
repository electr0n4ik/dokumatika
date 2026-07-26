"""Тесты слоя представления: экранирование, SEO-инварианты, реестр страниц.

Часть тестов пробегает по ВСЕМ страницам сразу и проверяет то, что легко забыть
на одной из пятнадцати: канонический адрес, длину title, наличие FAQ, отсутствие
запрещённых юридических формулировок. Такой тест дешевле любого чек-листа.
"""

from __future__ import annotations

import json
import re

import pytest

from app.config import RuntimeConfig, SiteConfig
from app.products import KOMPLEKT_152FZ
from app.web.html import Raw, attrs, esc, join, strip_tags
from app.web.layout import metrika_snippet, render_error, render_maintenance, render_page
from app.web.pages import PAGES, PAGES_BY_PATH, sitemap_entries
from app.web.pages.base import PageContext
from app.web.seo import (
    Crumb,
    FaqItem,
    PageMeta,
    breadcrumbs_schema,
    faq_schema,
    head_meta,
    json_ld,
    organization_schema,
    product_schema,
    web_application_schema,
)

# RU: Заявления, которые нельзя делать: они недостоверны и создают правовой риск
# по ст. 5 ФЗ «О рекламе».
FORBIDDEN_CLAIMS = (
    "юридическая консультация",
    "юридической консультацией",
    "одобрено роскомнадзором",
    "гарантируем отсутствие штрафов",
    "100% соответствие",
    "полностью соответствует закону",
)


@pytest.fixture()
def context(site: SiteConfig, runtime: RuntimeConfig) -> PageContext:
    return PageContext(site=site, runtime=runtime, product=KOMPLEKT_152FZ, payments_enabled=True)


class TestEscaping:
    def test_escapes_html_special_chars(self) -> None:
        assert esc('<script>"x"</script>') == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;"

    def test_raw_passes_through(self) -> None:
        assert esc(Raw("<b>жирный</b>")) == "<b>жирный</b>"

    def test_none_becomes_empty(self) -> None:
        assert esc(None) == ""

    def test_join_keeps_ready_markup(self) -> None:
        """join склеивает готовые фрагменты — экранирование делает вызывающий код."""
        assert join([f"<li>{esc('<b>')}</li>", Raw("<i></i>")]) == "<li>&lt;b&gt;</li><i></i>"

    def test_attrs_skips_none_and_false(self) -> None:
        assert attrs(id="x", hidden=None, disabled=False) == ' id="x"'

    def test_attrs_boolean_true(self) -> None:
        assert attrs(disabled=True) == " disabled"

    def test_attrs_renames_underscores(self) -> None:
        assert attrs(data_role="tab", for_="x") == ' data-role="tab" for="x"'

    def test_attrs_escapes_quotes(self) -> None:
        assert '"' not in attrs(title='a"b').split("=", 1)[1][1:-1]

    def test_strip_tags(self) -> None:
        assert strip_tags("<em>Политика</em> 152-ФЗ") == "Политика 152-ФЗ"


class TestJsonLd:
    def test_escapes_closing_script_tag(self) -> None:
        """Иначе значение вида </script> закрыло бы тег и стало исполняемым."""
        payload = json_ld({"name": "</script><img onerror=alert(1)>"})
        assert "</script><img" not in str(payload)
        assert "<\\/script>" in str(payload)

    def test_organization_schema_valid_json(self, site: SiteConfig) -> None:
        raw = str(json_ld(organization_schema(site)))
        body = raw.split(">", 1)[1].rsplit("<", 1)[0]
        assert json.loads(body.replace("<\\/", "</"))["@type"] == "Organization"

    def test_breadcrumbs_positions_sequential(self, site: SiteConfig) -> None:
        schema = breadcrumbs_schema(site, (Crumb("Главная", "/"), Crumb("Штрафы")))
        positions = [item["position"] for item in schema["itemListElement"]]
        assert positions == [1, 2]

    def test_last_crumb_has_no_link(self, site: SiteConfig) -> None:
        schema = breadcrumbs_schema(site, (Crumb("Главная", "/"), Crumb("Штрафы")))
        assert "item" not in schema["itemListElement"][1]

    def test_empty_crumbs_return_none(self, site: SiteConfig) -> None:
        assert breadcrumbs_schema(site, ()) is None

    def test_faq_schema_shape(self) -> None:
        schema = faq_schema((FaqItem("Вопрос?", "Ответ."),))
        assert schema["mainEntity"][0]["acceptedAnswer"]["text"] == "Ответ."

    def test_web_application_price(self, site: SiteConfig) -> None:
        schema = web_application_schema(
            site, path="/", name="Генератор", description="Описание", updated="26.07.2026"
        )
        assert schema["offers"]["price"] == "0"
        assert schema["dateModified"] == "2026-07-26"

    def test_product_schema_price_from_minor(self, site: SiteConfig) -> None:
        schema = product_schema(
            site, path="/komplekt/", name="Комплект", description="x", price_minor=79900
        )
        assert schema["offers"]["price"] == "799"
        assert schema["offers"]["priceCurrency"] == "RUB"


class TestHeadMeta:
    def test_canonical_is_absolute(self, site: SiteConfig) -> None:
        html = str(head_meta(site, PageMeta(path="/komplekt/", title="T", description="D")))
        assert 'rel="canonical" href="https://dokumatika.ru/komplekt/"' in html

    def test_noindex_emitted_only_when_asked(self, site: SiteConfig) -> None:
        meta = PageMeta(path="/", title="T", description="D")
        assert "noindex" not in str(head_meta(site, meta))
        assert "noindex" in str(head_meta(site, PageMeta(path="/", title="T", description="D", noindex=True)))

    def test_title_suffix_applied(self, site: SiteConfig) -> None:
        html = str(head_meta(site, PageMeta(path="/", title="Генератор", description="D")))
        assert "<title>Генератор — Докуматика</title>" in html

    def test_description_escaped(self, site: SiteConfig) -> None:
        html = str(head_meta(site, PageMeta(path="/", title="T", description='Он сказал "нет"')))
        assert 'content="Он сказал &quot;нет&quot;"' in html


class TestLayout:
    def test_page_is_complete_document(self, site: SiteConfig, runtime: RuntimeConfig) -> None:
        html = render_page(
            site=site,
            runtime=runtime,
            meta=PageMeta(path="/", title="T", description="D"),
            body=Raw("<p>тело</p>"),
        )
        assert html.startswith("<!doctype html>")
        assert '<html lang="ru"' in html and html.rstrip().endswith("</html>")
        assert "<p>тело</p>" in html

    def test_assets_are_versioned(self, site: SiteConfig, runtime: RuntimeConfig) -> None:
        """Без ?v= правка стилей не доедет до посетителя из-за годового кэша."""
        html = render_page(
            site=site, runtime=runtime, meta=PageMeta(path="/", title="T", description="D"), body=Raw("")
        )
        assert f"/styles.css?v={runtime.asset_version}" in html

    def test_scripts_versioned_and_deferred(self, site: SiteConfig, runtime: RuntimeConfig) -> None:
        html = render_page(
            site=site,
            runtime=runtime,
            meta=PageMeta(path="/", title="T", description="D"),
            body=Raw(""),
            extra_scripts=("wizard.js",),
        )
        assert f'src="/js/wizard.js?v={runtime.asset_version}" defer' in html

    def test_no_external_requests(self, site: SiteConfig, runtime: RuntimeConfig) -> None:
        """Ноль чужих хостов: скорость, приватность и отсутствие трансграничной передачи.

        Свой домен в canonical и og:url — не запрос, а ссылка на себя, поэтому
        проверяется именно чужое происхождение.
        """
        html = render_page(
            site=site, runtime=runtime, meta=PageMeta(path="/", title="T", description="D"), body=Raw("")
        )
        urls = re.findall(r'(?:href|src)="(https?://[^"]+)"', html)
        external = [url for url in urls if site.domain not in url]
        assert external == []

    def test_metrika_absent_without_id(self) -> None:
        assert metrika_snippet("") == ""

    def test_metrika_present_with_id(self) -> None:
        assert "mc.yandex.ru" in str(metrika_snippet("12345678"))

    def test_maintenance_page_is_noindex(self, site: SiteConfig) -> None:
        assert 'name="robots" content="noindex"' in render_maintenance(site)

    def test_error_page_noindex(self, site: SiteConfig, runtime: RuntimeConfig) -> None:
        html = render_error(site, runtime, code=404, title="Не найдено", message="Нет такой страницы")
        assert "noindex" in html and "404" in html

    def test_skiplink_present(self, site: SiteConfig, runtime: RuntimeConfig) -> None:
        html = render_page(
            site=site, runtime=runtime, meta=PageMeta(path="/", title="T", description="D"), body=Raw("")
        )
        assert 'class="skiplink"' in html and 'id="main"' in html


class TestPageRegistry:
    def test_paths_unique(self) -> None:
        paths = [page.path for page in PAGES]
        assert len(paths) == len(set(paths))

    def test_expected_pages_present(self) -> None:
        required = {
            "/",
            "/komplekt/",
            "/uvedomlenie-rkn/",
            "/shtrafy-152-fz/",
            "/152-fz-dlya-sayta/",
            "/soglasie/",
            "/cookie/",
            "/oferta/",
            "/privacy/",
            "/vozvrat/",
            "/kontakty/",
        }
        assert required <= set(PAGES_BY_PATH)

    def test_sitemap_entries_cover_indexable_pages(self) -> None:
        entries = {path for path, _, _ in sitemap_entries()}
        assert entries == {page.path for page in PAGES if page.in_sitemap}

    def test_home_has_wizard_scripts(self) -> None:
        assert "wizard.js" in PAGES_BY_PATH["/"].scripts

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_page_builds(self, page, context: PageContext) -> None:
        meta, body = page.build(context)
        assert isinstance(meta, PageMeta)
        assert str(body).strip(), f"{page.path}: пустое тело"

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_meta_matches_path(self, page, context: PageContext) -> None:
        meta, _ = page.build(context)
        assert meta.path == page.path

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_title_and_description_sane(self, page, context: PageContext) -> None:
        """Длина считается по тому, что реально попадёт в <title> — вместе с суффиксом."""
        meta, _ = page.build(context)
        full = meta.full_title + context.site.title_suffix
        assert 15 <= len(full) <= 70, f"{page.path}: title {len(full)} симв. — {full!r}"
        assert 50 <= len(meta.description) <= 200, f"{page.path}: description {len(meta.description)} симв."

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_no_escaped_markup(self, page, context: PageContext) -> None:
        """Страховка от двойного экранирования.

        Если разметку случайно пропустить через ``esc`` ещё раз, страница не
        падает — она просто показывает пользователю теги текстом. Такое легко
        не заметить в коде и невозможно не заметить в браузере, поэтому ловим
        автоматически.
        """
        meta, body = page.build(context)
        html = render_page(
            site=context.site, runtime=context.runtime, meta=meta, body=body, extra_scripts=page.scripts
        )
        leaked = re.findall(r"&lt;/?(?:a|div|table|tr|td|th|li|ul|ol|section|details|summary|script)\b", html)
        assert not leaked, f"{page.path}: разметка экранирована {len(leaked)} раз (потерян Raw)"

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_single_h1(self, page, context: PageContext) -> None:
        _, body = page.build(context)
        assert str(body).count("<h1") == 1, f"{page.path}: должен быть ровно один H1"

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_no_forbidden_claims(self, page, context: PageContext) -> None:
        meta, body = page.build(context)
        haystack = (str(body) + meta.title + meta.description).lower()
        found = [claim for claim in FORBIDDEN_CLAIMS if claim in haystack]
        # RU: «не является юридической консультацией» — допустимая формулировка,
        # запрещено обратное утверждение.
        found = [claim for claim in found if f"не является {claim}" not in haystack]
        assert not found, f"{page.path}: недопустимые заявления {found}"

    @pytest.mark.parametrize("page", PAGES, ids=lambda page: page.path)
    def test_renders_into_full_document(self, page, context: PageContext) -> None:
        meta, body = page.build(context)
        html = render_page(
            site=context.site,
            runtime=context.runtime,
            meta=meta,
            body=body,
            extra_scripts=page.scripts,
        )
        assert html.count("<!doctype html>") == 1
        assert "&lt;h1" not in html, f"{page.path}: разметка попала в текст (потерян Raw)"

    def test_content_pages_have_faq(self, context: PageContext) -> None:
        """FAQ даёт быстрые ответы в Яндексе — на контентных страницах он обязателен."""
        content_paths = {"/", "/komplekt/", "/uvedomlenie-rkn/", "/shtrafy-152-fz/", "/152-fz-dlya-sayta/",
                         "/soglasie/", "/cookie/"}
        for path in content_paths & set(PAGES_BY_PATH):
            meta, _ = PAGES_BY_PATH[path].build(context)
            assert len(meta.faq) >= 3, f"{path}: нужно минимум 3 вопроса FAQ"

    def test_content_pages_have_breadcrumbs(self, context: PageContext) -> None:
        for page in PAGES:
            if page.path == "/":
                continue
            meta, _ = page.build(context)
            assert meta.crumbs, f"{page.path}: нет хлебных крошек"


class TestPaymentsDisabledState:
    def test_komplekt_page_survives_disabled_payments(
        self, site: SiteConfig, runtime: RuntimeConfig
    ) -> None:
        """Рубильник оплаты не должен ронять страницу — только менять кнопку."""
        context = PageContext(
            site=site, runtime=runtime, product=KOMPLEKT_152FZ, payments_enabled=False
        )
        meta, body = PAGES_BY_PATH["/komplekt/"].build(context)
        assert str(body).strip() and meta.path == "/komplekt/"

    def test_seller_requisites_never_invented(self, runtime: RuntimeConfig) -> None:
        """Пустые реквизиты обязаны остаться пустыми, а не превратиться в выдумку."""
        blank_site = SiteConfig()
        context = PageContext(site=blank_site, runtime=runtime, product=KOMPLEKT_152FZ)
        for path in ("/oferta/", "/kontakty/"):
            if path not in PAGES_BY_PATH:
                continue
            _, body = PAGES_BY_PATH[path].build(context)
            text = str(body)
            assert not re.search(r"\bИНН\s+\d{10,12}\b", text), f"{path}: выдуманный ИНН"
