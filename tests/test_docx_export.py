"""Проверка, что сгенерированный .docx действительно открывается.

DOCX — это ZIP с XML внутри, и собирается он у нас вручную на чистом JS, без
библиотек. Ошибка в CRC32, в смещениях центрального каталога или в экранировании
кириллицы даст файл, который Word откажется открыть. Покупатель заметит это
раньше нас — и попросит деньги назад.

Поэтому здесь документ реально генерируется в Node, а затем открывается
средствами Python: проверяется структура архива, контрольные суммы и то, что
XML разбирается и содержит русский текст.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from app.documents.registry import ALL_DOCUMENTS
from app.documents.wizard import wizard_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JS_DIR = PROJECT_ROOT / "src" / "static" / "js"
NODE = shutil.which("node") or shutil.which("nodejs")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js не установлен")

ANSWERS = {
    "resource": "shop",
    "has_forms": True,
    "operator_type": "ip",
    "operator_name": "Иванов Иван Иванович",
    "inn": "770123456789",
    "ogrn": "304770000000001",
    "data_types": ["name", "email", "phone", "cookies", "payment", "address"],
    "purposes": ["feedback", "contract", "order", "analytics", "marketing"],
    "third_parties": ["hosting", "payment_service"],
    "cross_border": False,
    "site_url": "https://example.ru",
    "contact_email": "privacy@example.ru",
    "city": "Москва",
    "responsible_person": "Иванов И. И.",
}

DRIVER = textwrap.dedent(
    """
    const fs = require('fs');
    const noop = () => {};
    const el = {
      style: {}, dataset: {}, classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
      appendChild: noop, removeChild: noop, setAttribute: noop, getAttribute: () => null,
      addEventListener: noop, removeEventListener: noop, querySelector: () => null,
      querySelectorAll: () => [], insertAdjacentHTML: noop, remove: noop, children: [],
      get textContent() { return ''; }, set textContent(v) {},
      get innerHTML() { return ''; }, set innerHTML(v) {},
    };
    globalThis.window = globalThis;
    globalThis.document = {
      getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
      createElement: () => Object.create(el), createDocumentFragment: () => Object.create(el),
      addEventListener: noop, readyState: 'complete',
      body: Object.create(el), documentElement: Object.create(el),
    };
    const store = new Map();
    globalThis.localStorage = {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k),
    };
    globalThis.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    globalThis.navigator = { userAgent: 'node' };
    globalThis.location = { href: 'https://dokumatika.ru/', pathname: '/' };
    globalThis.Blob = class Blob {
      constructor(parts) { this._parts = parts; }
      async arrayBuffer() {
        const chunks = [];
        for (const part of this._parts) {
          if (typeof part === 'string') chunks.push(Buffer.from(part, 'utf8'));
          else if (part instanceof Uint8Array) chunks.push(Buffer.from(part));
          else if (part instanceof ArrayBuffer) chunks.push(Buffer.from(new Uint8Array(part)));
          else chunks.push(Buffer.from(String(part), 'utf8'));
        }
        return Buffer.concat(chunks);
      }
    };
    globalThis.URL = { createObjectURL: () => 'blob:stub', revokeObjectURL: noop };

    for (const file of process.argv[2].split(',')) {
      (0, eval)(fs.readFileSync(file, 'utf8'));
    }

    const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
    const outDir = process.argv[4];
    const api = globalThis.Dokumatika || {};
    // RU: экспорт живёт в поддереве docgen; допускаем и плоский вариант.
    const docgen = api.docgen || api;
    if (typeof docgen.toDocx !== 'function' || typeof api.renderDocument !== 'function') {
      console.log(JSON.stringify({ error: 'missing_api', available: Object.keys(api) }));
      process.exit(0);
    }

    (async () => {
      const values = api.computeValues(input.answers, input.valueRules, input.questions);
      const written = [];
      for (const template of input.templates) {
        const doc = api.renderDocument(template, input.answers, values);
        let blob = docgen.toDocx(doc);
        if (blob && typeof blob.then === 'function') blob = await blob;
        let buffer;
        if (blob instanceof Uint8Array) buffer = Buffer.from(blob);
        else if (blob && typeof blob.arrayBuffer === 'function') buffer = await blob.arrayBuffer();
        else if (typeof blob === 'string') buffer = Buffer.from(blob, 'binary');
        else {
          console.log(JSON.stringify({ error: 'bad_docx_type', code: template.code, type: typeof blob }));
          process.exit(0);
        }
        const path = outDir + '/' + template.code + '.docx';
        fs.writeFileSync(path, buffer);
        written.push({ code: template.code, path, bytes: buffer.length });
      }
      console.log(JSON.stringify({ written }));
    })();
    """
).strip()


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> dict:
    files = [JS_DIR / name for name in ("docgen.js", "wizard.js", "package.js") if (JS_DIR / name).exists()]
    if not files:
        pytest.skip("JS-файлы отсутствуют")

    tmp = tmp_path_factory.mktemp("docx")
    (tmp / "driver.cjs").write_text(DRIVER, encoding="utf-8")
    payload = wizard_payload()
    (tmp / "input.json").write_text(
        json.dumps(
            {
                "answers": ANSWERS,
                "valueRules": payload["valueRules"],
                "questions": payload["questions"],
                "templates": [document.to_dict() for document in ALL_DOCUMENTS],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_dir = tmp / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            NODE,
            str(tmp / "driver.cjs"),
            ",".join(str(f) for f in files),
            str(tmp / "input.json"),
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"Node упал:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pytest.fail(f"Node не вернул JSON:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
    if payload.get("error"):
        pytest.fail(f"Генератор DOCX недоступен: {payload}")
    return payload


def test_all_documents_produced(generated: dict) -> None:
    codes = {item["code"] for item in generated["written"]}
    assert codes == {document.code for document in ALL_DOCUMENTS}


def test_files_are_not_empty(generated: dict) -> None:
    for item in generated["written"]:
        assert item["bytes"] > 500, f"{item['code']}: {item['bytes']} байт — подозрительно мало"


@pytest.mark.parametrize("code", [document.code for document in ALL_DOCUMENTS])
def test_docx_is_valid_zip(generated: dict, code: str) -> None:
    """Открываем как ZIP и сверяем CRC каждого элемента — то же делает Word."""
    path = next(item["path"] for item in generated["written"] if item["code"] == code)
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        assert broken is None, f"{code}: битая запись {broken}"
        names = set(archive.namelist())
        for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            assert required in names, f"{code}: в архиве нет {required}"


@pytest.mark.parametrize("code", [document.code for document in ALL_DOCUMENTS])
def test_document_xml_parses(generated: dict, code: str) -> None:
    path = next(item["path"] for item in generated["written"] if item["code"] == code)
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    assert root.tag.endswith("document")
    assert root.find(f"{namespace}body") is not None, f"{code}: нет тела документа"


@pytest.mark.parametrize("code", [document.code for document in ALL_DOCUMENTS])
def test_cyrillic_survives_roundtrip(generated: dict, code: str) -> None:
    """Кириллица — главный риск ручной сборки XML: кодировка и экранирование."""
    path = next(item["path"] for item in generated["written"] if item["code"] == code)
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    text = "".join(node.text or "" for node in ET.fromstring(xml).iter(f"{namespace}t"))
    assert any("а" <= char <= "я" for char in text.lower()), f"{code}: русского текста нет"
    assert "Иванов Иван Иванович" in text, f"{code}: подстановка значений не доехала"
    assert "[не заполнено" not in text, f"{code}: дыра в документе при полных ответах"


def test_content_types_declares_document(generated: dict) -> None:
    path = generated["written"][0]["path"]
    with zipfile.ZipFile(path) as archive:
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
    assert "wordprocessingml.document.main+xml" in content_types
