"""Паритет движка документов между Python и JavaScript.

Самый важный тест проекта. Документ собирается дважды: на сервере (для
предпросмотра, тестов и проверок) и в браузере (для реальной выдачи
пользователю). Обе реализации написаны отдельно — на Python и на JS, — и любое
расхождение означает, что человек скачает документ, отличающийся от того, что мы
проверяли. Найти такое в проде почти невозможно.

Поэтому здесь один и тот же набор ответов прогоняется через обе реализации, и
результаты сравниваются буквально.

Если Node недоступен, тесты пропускаются: это проверка целостности, а не
обязательное условие сборки.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from app.documents.registry import ALL_DOCUMENTS
from app.documents.schema import render_document
from app.documents.wizard import compute_values, wizard_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JS_DIR = PROJECT_ROOT / "src" / "static" / "js"

NODE = shutil.which("node") or shutil.which("nodejs")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js не установлен — паритет не проверяем")


# RU: Наборы ответов подобраны так, чтобы задеть все ветки условий: пустые
# ответы, минимум, максимум и «редкие» флаги вроде трансграничной передачи.
FIXTURES: dict[str, dict] = {
    "empty": {},
    "minimal": {
        "resource": "site",
        "has_forms": True,
        "operator_type": "individual",
        "operator_name": "Петров Пётр Петрович",
        "inn": "770000000000",
        "data_types": ["name", "email"],
        "purposes": ["feedback"],
        "site_url": "https://example.ru",
        "contact_email": "privacy@example.ru",
    },
    "shop_full": {
        "resource": "shop",
        "has_forms": True,
        "operator_type": "ip",
        "operator_name": "Иванов Иван Иванович",
        "inn": "770123456789",
        "ogrn": "304770000000001",
        "data_types": ["name", "email", "phone", "cookies", "payment", "address", "social"],
        "purposes": ["feedback", "contract", "order", "analytics", "marketing", "support"],
        "third_parties": ["hosting", "analytics_service", "payment_service", "delivery_service", "crm"],
        "cross_border": True,
        "site_url": "https://shop.example.ru",
        "contact_email": "pd@shop.example.ru",
        "city": "Москва",
        "responsible_person": "Иванов И. И.",
        "doc_date": "2026-01-09",
    },
    "company": {
        "resource": "app",
        "has_forms": False,
        "operator_type": "ooo",
        "operator_name": "ООО «Ромашка»",
        "inn": "7701234567",
        "ogrn": "1027700000001",
        "data_types": ["name", "phone", "birthdate", "passport"],
        "purposes": ["contract"],
        "third_parties": ["none"],
        "cross_border": False,
        "site_url": "https://app.example.ru",
        "contact_email": "legal@example.ru",
    },
    "bot": {
        "resource": "bot",
        "has_forms": True,
        "operator_type": "self_employed",
        "operator_name": "Сидоров Сидор Сидорович",
        "inn": "770987654321",
        "data_types": ["name", "social"],
        "purposes": ["feedback", "marketing"],
        "site_url": "https://t.me/example_bot",
        "contact_email": "bot@example.ru",
        "city": "Казань",
    },
}


def _driver_source() -> str:
    """Скрипт-переходник: грузит наши файлы в Node и печатает результат JSON.

    Минимальные заглушки ``window``/``document``/``localStorage`` нужны потому,
    что файлы писались для браузера и при загрузке трогают DOM.
    """
    return textwrap.dedent(
        """
        const fs = require('fs');

        const noop = () => {};
        const fakeElement = {
          style: {}, dataset: {}, classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
          appendChild: noop, removeChild: noop, setAttribute: noop, getAttribute: () => null,
          addEventListener: noop, removeEventListener: noop, querySelector: () => null,
          querySelectorAll: () => [], insertAdjacentHTML: noop, remove: noop,
          get textContent() { return ''; }, set textContent(v) {},
          get innerHTML() { return ''; }, set innerHTML(v) {},
          children: [], firstChild: null, parentNode: null,
        };
        const fakeDocument = {
          getElementById: () => null,
          querySelector: () => null,
          querySelectorAll: () => [],
          createElement: () => Object.create(fakeElement),
          createDocumentFragment: () => Object.create(fakeElement),
          addEventListener: noop,
          readyState: 'complete',
          body: Object.create(fakeElement),
          documentElement: Object.create(fakeElement),
        };
        const storage = new Map();
        globalThis.window = globalThis;
        globalThis.document = fakeDocument;
        globalThis.localStorage = {
          getItem: (k) => (storage.has(k) ? storage.get(k) : null),
          setItem: (k, v) => storage.set(k, String(v)),
          removeItem: (k) => storage.delete(k),
        };
        globalThis.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
        globalThis.navigator = { userAgent: 'node' };
        globalThis.location = { href: 'https://dokumatika.ru/', pathname: '/' };

        const files = process.argv[2].split(',');
        for (const file of files) {
          const code = fs.readFileSync(file, 'utf8');
          (0, eval)(code);
        }

        const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
        const api = globalThis.Dokumatika || {};
        const missing = ['computeValues', 'renderDocument'].filter((name) => typeof api[name] !== 'function');
        if (missing.length) {
          console.log(JSON.stringify({ error: 'missing_api', missing, available: Object.keys(api) }));
          process.exit(0);
        }

        const out = { values: {}, documents: {} };
        for (const [name, answers] of Object.entries(input.fixtures)) {
          const values = api.computeValues(answers, input.valueRules, input.questions);
          out.values[name] = values;
          out.documents[name] = {};
          for (const template of input.templates) {
            const doc = api.renderDocument(template, answers, values);
            out.documents[name][template.code] = {
              title: doc.title,
              clauses: (doc.clauses || []).map((c) => ({
                id: c.id,
                title: c.title || '',
                paragraphs: c.paragraphs || [],
              })),
            };
          }
        }
        console.log(JSON.stringify(out));
        """
    ).strip()


def _js_files() -> list[Path]:
    order = ["docgen.js", "wizard.js", "package.js"]
    return [JS_DIR / name for name in order if (JS_DIR / name).exists()]


@pytest.fixture(scope="module")
def js_output(tmp_path_factory) -> dict:
    files = _js_files()
    if not files:
        pytest.skip("JS-файлы ещё не созданы")

    tmp = tmp_path_factory.mktemp("jsparity")
    driver = tmp / "driver.cjs"
    driver.write_text(_driver_source(), encoding="utf-8")

    payload = wizard_payload()
    input_file = tmp / "input.json"
    input_file.write_text(
        json.dumps(
            {
                "fixtures": FIXTURES,
                "valueRules": payload["valueRules"],
                "questions": payload["questions"],
                "templates": [document.to_dict() for document in ALL_DOCUMENTS],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [NODE, str(driver), ",".join(str(path) for path in files), str(input_file)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        pytest.fail(f"Node упал:\nstdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}")

    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.fail(f"Node не вернул JSON:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")


def test_js_exposes_document_engine(js_output: dict) -> None:
    """Клиент обязан отдавать движок наружу — иначе паритет непроверяем."""
    if js_output.get("error") == "missing_api":
        pytest.fail(
            "window.Dokumatika не содержит computeValues/renderDocument. "
            f"Не хватает: {js_output.get('missing')}. Доступно: {js_output.get('available')}"
        )


@pytest.mark.parametrize("fixture_name", sorted(FIXTURES))
def test_values_match(js_output: dict, fixture_name: str) -> None:
    if js_output.get("error"):
        pytest.skip("движок не экспортирован")
    expected = compute_values(FIXTURES[fixture_name])
    actual = js_output["values"][fixture_name]
    # RU: doc_date по умолчанию — сегодняшняя дата; в обеих средах она совпадёт,
    # но на границе суток может разъехаться, поэтому сравниваем отдельно.
    for key in sorted(expected):
        if key == "doc_date" and not FIXTURES[fixture_name].get("doc_date"):
            continue
        assert actual.get(key) == expected[key], f"{fixture_name}.{key}"


@pytest.mark.parametrize("fixture_name", sorted(FIXTURES))
def test_documents_match(js_output: dict, fixture_name: str) -> None:
    if js_output.get("error"):
        pytest.skip("движок не экспортирован")
    answers = FIXTURES[fixture_name]
    values = compute_values(answers)
    rendered_js = js_output["documents"][fixture_name]

    for template in ALL_DOCUMENTS:
        expected = render_document(template, answers, values)
        actual = rendered_js.get(template.code)
        assert actual is not None, f"{fixture_name}: JS не собрал {template.code}"

        expected_ids = [clause.id for clause in expected.clauses]
        actual_ids = [clause["id"] for clause in actual["clauses"]]
        assert actual_ids == expected_ids, (
            f"{fixture_name}/{template.code}: разный набор пунктов.\n"
            f"только в Python: {sorted(set(expected_ids) - set(actual_ids))}\n"
            f"только в JS: {sorted(set(actual_ids) - set(expected_ids))}"
        )

        for expected_clause, actual_clause in zip(expected.clauses, actual["clauses"], strict=False):
            if template.code == "policy" or not answers.get("doc_date"):
                # RU: пункты с датой по умолчанию сравниваем без неё.
                pass
            assert actual_clause["title"] == expected_clause.title, (
                f"{fixture_name}/{template.code}/{expected_clause.id}: разный заголовок"
            )
            assert list(actual_clause["paragraphs"]) == list(expected_clause.paragraphs), (
                f"{fixture_name}/{template.code}/{expected_clause.id}: разный текст"
            )
