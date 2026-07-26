"""Каркас страницы.

Три принципа, заложенные в разметку:

1. **Ноль внешних запросов.** Никаких Google Fonts и CDN: шрифты системные.
   Это и скорость (Lighthouse mobile 95+ достижим без ухищрений), и отсутствие
   передачи IP посетителей на зарубежные серверы — тема, которой в политике
   пришлось бы объясняться.
2. **JS необязателен.** Страницы читаются и индексируются без единого скрипта;
   визард — прогрессивное улучшение поверх готового HTML.
3. **Версионированная статика.** Все ссылки на css/js идут с ``?v=``, поэтому
   nginx отдаёт их с immutable-кэшем на год, а правка стилей не требует ждать
   истечения кэша у посетителя.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import RuntimeConfig, SiteConfig
from .components import site_footer, site_header
from .html import Raw, esc, join
from .seo import PageMeta, head_meta, json_ld, organization_schema, page_schemas, website_schema

# RU: Системный стек — под Windows/macOS/Linux/Android даёт близкий вид без загрузки.
FONT_NOTE = "system-ui"


def metrika_snippet(metrika_id: str) -> Raw:
    """Счётчик Метрики. Пустой ID -> ничего не рендерим (dev/preview чистые)."""
    if not metrika_id:
        return Raw("")
    ident = esc(metrika_id)
    return Raw(
        "<script>"
        "(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};"
        "m[i].l=1*new Date();for(var j=0;j<document.scripts.length;j++){"
        "if(document.scripts[j].src===r){return}}"
        "k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,"
        "a.parentNode.insertBefore(k,a)})"
        "(window,document,'script','https://mc.yandex.ru/metrika/tag.js','ym');"
        f"ym({ident},'init',{{clickmap:true,trackLinks:true,accurateTrackBounce:true}});"
        "</script>"
        f'<noscript><div><img src="https://mc.yandex.ru/watch/{ident}" '
        'style="position:absolute;left:-9999px" alt=""></div></noscript>'
    )


def render_page(
    *,
    site: SiteConfig,
    runtime: RuntimeConfig,
    meta: PageMeta,
    body: Raw | str,
    extra_scripts: tuple[str, ...] = (),
    body_class: str = "",
) -> str:
    version = esc(runtime.asset_version)
    schemas = (organization_schema(site), website_schema(site), *page_schemas(site, meta))
    schema_html = join([json_ld(schema) for schema in schemas])
    scripts = join(
        [f'<script src="/js/{esc(name)}?v={version}" defer></script>' for name in extra_scripts]
    )
    year = datetime.now(timezone.utc).year
    body_attr = f' class="{esc(body_class)}"' if body_class else ""

    return (
        "<!doctype html>"
        f'<html lang="ru" data-theme="{esc(site.theme)}">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{esc(head_meta(site, meta))}"
        f'<link rel="icon" type="image/svg+xml" href="/favicon.svg?v={version}">'
        f'<link rel="stylesheet" href="/styles.css?v={version}">'
        f"{esc(schema_html)}"
        "</head>"
        f"<body{body_attr}>"
        '<a class="skiplink" href="#main">Перейти к содержимому</a>'
        f'<div class="shell">{esc(site_header(site, current_path=meta.path))}</div>'
        f'<main class="shell" id="main">{esc(body)}</main>'
        f"{esc(site_footer(site, year))}"
        f"{esc(scripts)}"
        f"{esc(metrika_snippet(site.metrika_id))}"
        "</body></html>"
    )


def render_maintenance(site: SiteConfig) -> str:
    """Заглушка мягкого kill-switch.

    Отдаётся с кодом 503 и ``Retry-After``: для поисковика это «зайди позже», а
    не «страница удалена», поэтому короткая пауза не стоит позиций.
    """
    return (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        "<title>Сайт временно недоступен</title>"
        "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;display:grid;"
        "place-items:center;min-height:100vh;margin:0;background:#F6F8FB;color:#0F172A}"
        ".box{text-align:center;padding:24px;max-width:420px}"
        "p{color:#64748B}</style></head>"
        '<body><div class="box"><h1>Сайт временно недоступен</h1>'
        "<p>Идут технические работы. Вернёмся в ближайшее время.</p>"
        f"<p>{esc(site.brand)}</p></div></body></html>"
    )


def render_error(site: SiteConfig, runtime: RuntimeConfig, *, code: int, title: str, message: str) -> str:
    meta = PageMeta(
        path="/",
        title=title,
        description=message,
        noindex=True,
    )
    body = Raw(
        f'<section class="errorpage"><span class="errorcode">{esc(code)}</span>'
        f"<h1>{esc(title)}</h1><p>{esc(message)}</p>"
        '<p><a class="btn btn-p" href="/">На главную</a></p></section>'
    )
    return render_page(site=site, runtime=runtime, meta=meta, body=body)
