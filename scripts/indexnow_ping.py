"""Пинг IndexNow: «мы обновились, приходите переиндексировать».

IndexNow — единый протокол, который понимают Яндекс и Bing. Вместо ожидания,
пока робот сам заглянет на сайт, мы отправляем список адресов и получаем обход
в течение минут-часов. Для нового сайта это единственный быстрый способ попасть
в индекс: сидеть и ждать краулер можно неделями.

Как это работает: на сайте лежит файл ``/<ключ>.txt`` с этим же ключом внутри
(его отдаёт само приложение, см. app/server.py), поисковик скачивает файл и
убеждается, что отправитель владеет доменом.

Запуск после выкладки новых страниц:

    python3 scripts/indexnow_ping.py --env-file /etc/dokumatika/.env
    python3 scripts/indexnow_ping.py --url https://dokumatika.ru/shtrafy-152-fz/

Без аргументов берёт все адреса из sitemap.xml боевого сайта.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.config import load_site_config  # noqa: E402

# RU: Оба принимают один и тот же формат. Yandex обслуживает Яндекс, api.indexnow.org
# раздаёт ключ остальным участникам протокола (Bing, Seznam, Naver).
INDEXNOW_ENDPOINTS = (
    "https://api.indexnow.org/indexnow",
    "https://yandex.com/indexnow",
)

# RU: 200 — принято, 202 — принято, ключ проверяется асинхронно. Оба успех.
SUCCESS_CODES = frozenset({200, 202})

# RU: Ограничение протокола на одну отправку.
MAX_URLS_PER_REQUEST = 10_000

USER_AGENT = "dokumatika-indexnow/1.0"

# RU: Расшифровка кодов из спецификации — иначе голый 422 ни о чём не говорит.
ERROR_HINTS = {
    400: "неверный формат запроса",
    403: "ключ не найден или не совпадает с содержимым файла-подтверждения",
    422: "адреса не принадлежат указанному хосту либо ключ не тот",
    429: "слишком часто; IndexNow нужен при реальных изменениях, а не по крону раз в минуту",
}


def load_env_file(path: Path) -> int:
    """Прочитать ``KEY=VALUE`` из .env в окружение процесса."""
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1
    return loaded


def local_name(tag: str) -> str:
    """``{http://...}loc`` -> ``loc``: имена в sitemap всегда в пространстве имён."""
    return tag.rsplit("}", 1)[-1]


def fetch(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read())


def read_sitemap(source: str, *, timeout: float, depth: int = 0) -> list[str]:
    """Достать адреса из sitemap.xml. Источник — URL или локальный файл."""
    if source.startswith(("http://", "https://")):
        raw = fetch(source, timeout=timeout)
    else:
        raw = Path(source).read_bytes()

    root = ElementTree.fromstring(raw)
    locations = [
        (element.text or "").strip()
        for element in root.iter()
        if local_name(element.tag) == "loc" and (element.text or "").strip()
    ]

    # RU: Если это индекс sitemap'ов, в loc лежат ссылки на другие карты —
    # спускаемся на уровень ниже, но ровно на один, чтобы не зациклиться.
    if local_name(root.tag) == "sitemapindex" and depth < 1:
        nested: list[str] = []
        for child_sitemap in locations:
            nested.extend(read_sitemap(child_sitemap, timeout=timeout, depth=depth + 1))
        return nested
    return locations


def submit(endpoint: str, payload: dict[str, object], *, timeout: float) -> tuple[bool, str]:
    """Отправить список адресов. Возвращает (успех, человекочитаемый ответ)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = int(response.status)
            return code in SUCCESS_CODES, f"HTTP {code}"
    except urllib.error.HTTPError as error:
        hint = ERROR_HINTS.get(int(error.code), "")
        detail = f"HTTP {error.code}"
        if hint:
            detail += f" — {hint}"
        return int(error.code) in SUCCESS_CODES, detail
    except urllib.error.URLError as error:
        return False, f"сеть недоступна: {error.reason}"
    except OSError as error:  # pragma: no cover - таймаут и прочие сбои сокета
        return False, f"ошибка соединения: {error}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Отправить адреса сайта в IndexNow")
    parser.add_argument("--env-file", type=Path, default=None, help="путь к .env с INDEXNOW_KEY")
    parser.add_argument("--sitemap", default="", help="URL или файл sitemap.xml (по умолчанию — с сайта)")
    parser.add_argument("--url", action="append", default=[], help="конкретный адрес; можно повторять")
    parser.add_argument("--key", default="", help="ключ IndexNow, если не хочется брать из окружения")
    parser.add_argument("--timeout", type=float, default=20.0, help="таймаут запроса, секунд")
    parser.add_argument("--dry-run", action="store_true", help="показать, что отправили бы, и выйти")
    args = parser.parse_args(argv)

    if args.env_file is not None:
        if not args.env_file.is_file():
            print(f"Файл окружения не найден: {args.env_file}")
            return 2
        load_env_file(args.env_file)

    site = load_site_config()
    key = (args.key or site.indexnow_key).strip()
    if not key:
        print("INDEXNOW_KEY не задан. Сгенерируйте ключ и положите его в .env:")
        print("  python3 -c \"import secrets; print(secrets.token_hex(16))\"")
        return 2

    host = site.domain
    urls = [url.strip() for url in args.url if url.strip()]
    if not urls:
        sitemap = args.sitemap or site.url("/sitemap.xml")
        try:
            urls = read_sitemap(sitemap, timeout=args.timeout)
        except (OSError, urllib.error.URLError, ElementTree.ParseError) as error:
            print(f"Не удалось прочитать sitemap ({sitemap}): {error}")
            return 2
        print(f"Из {sitemap} прочитано адресов: {len(urls)}")

    # RU: Чужие адреса ломают всю отправку целиком (ответ 422), поэтому режем их
    # заранее и говорим об этом вслух.
    own = [url for url in urls if urlparse(url).hostname == host]
    foreign = len(urls) - len(own)
    if foreign:
        print(f"Пропущено адресов с другим хостом: {foreign}")
    if not own:
        print("Отправлять нечего.")
        return 2
    if len(own) > MAX_URLS_PER_REQUEST:
        print(f"Адресов больше {MAX_URLS_PER_REQUEST}, отправляю только первые.")
        own = own[:MAX_URLS_PER_REQUEST]

    payload = {
        "host": host,
        "key": key,
        # RU: keyLocation обязателен, если файл лежит не в корне; у нас он в
        # корне, но явное указание избавляет от догадок на стороне поисковика.
        "keyLocation": site.url(f"/{key}.txt"),
        "urlList": own,
    }

    print(f"Хост: {host}; адресов к отправке: {len(own)}")
    for url in own:
        print(f"  {url}")

    if args.dry_run:
        print("\n--dry-run: ничего не отправлено.")
        return 0

    accepted = 0
    for endpoint in INDEXNOW_ENDPOINTS:
        ok, detail = submit(endpoint, payload, timeout=args.timeout)
        marker = "ok" if ok else "!!"
        print(f"[{marker}] {endpoint}: {detail}")
        accepted += 1 if ok else 0

    if not accepted:
        print("\nНи один эндпоинт не принял список. Проверьте, что файл-подтверждения открывается:")
        print(f"  curl -sS {site.url('/' + key + '.txt')}")
        return 1
    print(f"\nПринято эндпоинтами: {accepted} из {len(INDEXNOW_ENDPOINTS)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
