/**
 * Экспорт документа: .docx, .html, plain text и печать — без единой библиотеки.
 *
 * .docx собирается здесь целиком: это ZIP из четырёх обязательных XML-частей
 * плюс styles.xml ради нормальных заголовков. Сжатие не используется (метод
 * STORE) — документ весит десятки килобайт, а реализация deflate на JS стоила
 * бы сотен строк и риска битого файла. Битый .docx = возврат денег, поэтому
 * весь ZIP-писатель написан по спецификации APPNOTE 4.3 и проверяется тестом.
 *
 * Кириллица во всех частях кодируется через TextEncoder (UTF-8), имена файлов
 * в архиве помечены флагом 0x0800 — иначе распаковщик прочитает их в CP437.
 */
(function () {
  'use strict';

  const D = (window.Dokumatika = window.Dokumatika || {});

  const A4_WIDTH = 11906; // твипы, ширина листа A4
  const MARGIN_LEFT = 1701;
  const MARGIN_RIGHT = 850;
  const TEXT_WIDTH = A4_WIDTH - MARGIN_LEFT - MARGIN_RIGHT;

  // -------------------------------------------------------------- байты и ZIP

  const encoder = typeof TextEncoder === 'function' ? new TextEncoder() : null;

  function utf8(text) {
    const value = String(text === null || text === undefined ? '' : text);
    if (encoder) return encoder.encode(value);
    // RU: Запасной путь для древних движков — ручной UTF-8.
    const bytes = [];
    for (let index = 0; index < value.length; index += 1) {
      let code = value.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff && index + 1 < value.length) {
        const low = value.charCodeAt(index + 1);
        if (low >= 0xdc00 && low <= 0xdfff) {
          code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
          index += 1;
        }
      }
      if (code < 0x80) {
        bytes.push(code);
      } else if (code < 0x800) {
        bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
      } else if (code < 0x10000) {
        bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
      } else {
        bytes.push(
          0xf0 | (code >> 18),
          0x80 | ((code >> 12) & 0x3f),
          0x80 | ((code >> 6) & 0x3f),
          0x80 | (code & 0x3f)
        );
      }
    }
    return new Uint8Array(bytes);
  }

  let crcTable = null;

  function crcLookup() {
    if (crcTable) return crcTable;
    const table = new Uint32Array(256);
    for (let index = 0; index < 256; index += 1) {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) {
        value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
      }
      table[index] = value >>> 0;
    }
    crcTable = table;
    return crcTable;
  }

  function crc32(bytes) {
    const table = crcLookup();
    let crc = 0xffffffff;
    for (let index = 0; index < bytes.length; index += 1) {
      crc = (table[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8)) >>> 0;
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  /** Дата и время в формате MS-DOS — обязательное поле заголовков ZIP. */
  function dosStamp(date) {
    const moment = date instanceof Date ? date : new Date();
    const year = Math.max(1980, moment.getFullYear());
    return {
      time: ((moment.getHours() << 11) | (moment.getMinutes() << 5) | (moment.getSeconds() >> 1)) & 0xffff,
      date: (((year - 1980) << 9) | ((moment.getMonth() + 1) << 5) | moment.getDate()) & 0xffff,
    };
  }

  function concatBytes(chunks) {
    let total = 0;
    chunks.forEach((chunk) => {
      total += chunk.length;
    });
    const result = new Uint8Array(total);
    let offset = 0;
    chunks.forEach((chunk) => {
      result.set(chunk, offset);
      offset += chunk.length;
    });
    return result;
  }

  /**
   * Собрать ZIP методом STORE. ``files`` — массив ``{name, data}``,
   * где data это Uint8Array или строка.
   */
  function zipStore(files) {
    const stamp = dosStamp(new Date());
    const chunks = [];
    const directory = [];
    let offset = 0;

    files.forEach((file) => {
      const nameBytes = utf8(file.name);
      const data = file.data instanceof Uint8Array ? file.data : utf8(file.data);
      const crc = crc32(data);

      const local = new Uint8Array(30 + nameBytes.length);
      const localView = new DataView(local.buffer);
      localView.setUint32(0, 0x04034b50, true); // сигнатура локального заголовка
      localView.setUint16(4, 20, true); // минимальная версия распаковщика
      localView.setUint16(6, 0x0800, true); // имя файла в UTF-8
      localView.setUint16(8, 0, true); // метод: без сжатия
      localView.setUint16(10, stamp.time, true);
      localView.setUint16(12, stamp.date, true);
      localView.setUint32(14, crc, true);
      localView.setUint32(18, data.length, true); // сжатый размер
      localView.setUint32(22, data.length, true); // исходный размер
      localView.setUint16(26, nameBytes.length, true);
      localView.setUint16(28, 0, true); // extra field
      local.set(nameBytes, 30);

      const entry = new Uint8Array(46 + nameBytes.length);
      const entryView = new DataView(entry.buffer);
      entryView.setUint32(0, 0x02014b50, true); // сигнатура записи каталога
      entryView.setUint16(4, 20, true); // версия создателя
      entryView.setUint16(6, 20, true); // минимальная версия распаковщика
      entryView.setUint16(8, 0x0800, true);
      entryView.setUint16(10, 0, true);
      entryView.setUint16(12, stamp.time, true);
      entryView.setUint16(14, stamp.date, true);
      entryView.setUint32(16, crc, true);
      entryView.setUint32(20, data.length, true);
      entryView.setUint32(24, data.length, true);
      entryView.setUint16(28, nameBytes.length, true);
      entryView.setUint16(30, 0, true); // extra
      entryView.setUint16(32, 0, true); // комментарий
      entryView.setUint16(34, 0, true); // номер диска
      entryView.setUint16(36, 0, true); // внутренние атрибуты
      entryView.setUint32(38, 0, true); // внешние атрибуты
      entryView.setUint32(42, offset, true); // смещение локального заголовка
      entry.set(nameBytes, 46);

      chunks.push(local, data);
      directory.push(entry);
      offset += local.length + data.length;
    });

    let directorySize = 0;
    directory.forEach((entry) => {
      directorySize += entry.length;
    });

    const end = new Uint8Array(22);
    const endView = new DataView(end.buffer);
    endView.setUint32(0, 0x06054b50, true); // сигнатура конца каталога
    endView.setUint16(4, 0, true); // номер диска
    endView.setUint16(6, 0, true); // диск с началом каталога
    endView.setUint16(8, directory.length, true);
    endView.setUint16(10, directory.length, true);
    endView.setUint32(12, directorySize, true);
    endView.setUint32(16, offset, true); // смещение начала каталога
    endView.setUint16(20, 0, true); // комментарий архива

    return concatBytes(chunks.concat(directory, [end]));
  }

  // ------------------------------------------------------------ экранирование

  function escapeXml(text) {
    const value = String(text === null || text === undefined ? '' : text);
    let result = '';
    for (let index = 0; index < value.length; index += 1) {
      const char = value[index];
      const code = value.charCodeAt(index);
      // RU: Управляющие символы в XML запрещены — молча выбрасываем.
      if (code < 0x20 && char !== '\n' && char !== '\t') continue;
      if (char === '&') result += '&amp;';
      else if (char === '<') result += '&lt;';
      else if (char === '>') result += '&gt;';
      else if (char === '"') result += '&quot;';
      else if (char === "'") result += '&apos;';
      else result += char;
    }
    return result;
  }

  function escapeHtml(text) {
    return escapeXml(text);
  }

  // ------------------------------------------------------------------- .docx

  const XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
  const W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
  const REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships';
  const OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';

  const CONTENT_TYPES =
    XML_HEAD
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    + '<Default Extension="rels" '
    + 'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    + 'officedocument.wordprocessingml.document.main+xml"/>'
    + '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
    + 'officedocument.wordprocessingml.styles+xml"/>'
    + '</Types>';

  const ROOT_RELS =
    XML_HEAD
    + '<Relationships xmlns="' + REL_NS + '">'
    + '<Relationship Id="rId1" Type="' + OFFICE_REL + '/officeDocument" Target="word/document.xml"/>'
    + '</Relationships>';

  const DOCUMENT_RELS =
    XML_HEAD
    + '<Relationships xmlns="' + REL_NS + '">'
    + '<Relationship Id="rId1" Type="' + OFFICE_REL + '/styles" Target="styles.xml"/>'
    + '</Relationships>';

  function styleXml(id, name, options) {
    const config = options || {};
    return (
      '<w:style w:type="paragraph" w:styleId="' + id + '">'
      + '<w:name w:val="' + name + '"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
      + '<w:pPr>'
      + (config.outline !== undefined ? '<w:outlineLvl w:val="' + config.outline + '"/><w:keepNext/>' : '')
      + '<w:spacing w:before="' + (config.before || 0) + '" w:after="' + (config.after || 120) + '"/>'
      + '<w:jc w:val="' + (config.align || 'left') + '"/>'
      + '</w:pPr>'
      + '<w:rPr><w:b/><w:sz w:val="' + (config.size || 28) + '"/>'
      + '<w:szCs w:val="' + (config.size || 28) + '"/></w:rPr>'
      + '</w:style>'
    );
  }

  const STYLES_XML =
    XML_HEAD
    + '<w:styles xmlns:w="' + W_NS + '">'
    + '<w:docDefaults><w:rPrDefault><w:rPr>'
    + '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>'
    + '<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="ru-RU"/>'
    + '</w:rPr></w:rPrDefault>'
    + '<w:pPrDefault><w:pPr>'
    + '<w:spacing w:after="120" w:line="276" w:lineRule="auto"/><w:jc w:val="both"/>'
    + '</w:pPr></w:pPrDefault></w:docDefaults>'
    + '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    + '<w:qFormat/></w:style>'
    + styleXml('Title', 'Title', { size: 32, align: 'center', after: 60 })
    + styleXml('Subtitle', 'Subtitle', { size: 24, align: 'center', after: 240 })
    + styleXml('Heading1', 'heading 1', { size: 28, outline: 0, before: 240 })
    + styleXml('Heading2', 'heading 2', { size: 26, outline: 1, before: 180 })
    + '</w:styles>';

  /** Абзац: текст + необязательные стиль, отступ и прямое оформление. */
  function paragraphXml(text, options) {
    const config = options || {};
    const properties = [];
    if (config.style) properties.push('<w:pStyle w:val="' + config.style + '"/>');
    if (config.indent) {
      properties.push('<w:ind w:left="' + config.indent + '" w:hanging="' + (config.hanging || 0) + '"/>');
    }
    if (config.align) properties.push('<w:jc w:val="' + config.align + '"/>');
    if (config.spacingAfter !== undefined) {
      properties.push('<w:spacing w:after="' + config.spacingAfter + '"/>');
    }

    const runProperties = [];
    if (config.bold) runProperties.push('<w:b/>');
    if (config.italic) runProperties.push('<w:i/>');
    if (config.size) {
      runProperties.push('<w:sz w:val="' + config.size + '"/><w:szCs w:val="' + config.size + '"/>');
    }

    // RU: Перевод строки внутри абзаца — это <w:br/>, а не новый параграф.
    const runs = String(text === null || text === undefined ? '' : text)
      .split('\n')
      .map((line) => '<w:t xml:space="preserve">' + escapeXml(line) + '</w:t>')
      .join('<w:br/>');

    return (
      '<w:p>'
      + (properties.length ? '<w:pPr>' + properties.join('') + '</w:pPr>' : '')
      + '<w:r>'
      + (runProperties.length ? '<w:rPr>' + runProperties.join('') + '</w:rPr>' : '')
      + runs
      + '</w:r></w:p>'
    );
  }

  function tableXml(rows) {
    const columns = rows.reduce((max, row) => Math.max(max, row.length), 1);
    const columnWidth = Math.floor(TEXT_WIDTH / columns);
    const borders = ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']
      .map((side) => '<w:' + side + ' w:val="single" w:sz="4" w:space="0" w:color="808080"/>')
      .join('');
    const grid = new Array(columns).fill('<w:gridCol w:w="' + columnWidth + '"/>').join('');

    const body = rows
      .map((row, rowIndex) => {
        const cells = [];
        for (let index = 0; index < columns; index += 1) {
          const text = row[index] === undefined ? '' : row[index];
          cells.push(
            '<w:tc><w:tcPr><w:tcW w:w="' + columnWidth + '" w:type="dxa"/></w:tcPr>'
            + paragraphXml(text, {
              align: 'left',
              spacingAfter: 0,
              bold: rows.length > 1 && rowIndex === 0,
            })
            + '</w:tc>'
          );
        }
        return '<w:tr>' + cells.join('') + '</w:tr>';
      })
      .join('');

    return (
      '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>' + borders + '</w:tblBorders>'
      + '</w:tblPr><w:tblGrid>' + grid + '</w:tblGrid>' + body + '</w:tbl>'
      // RU: После таблицы обязателен абзац, иначе Word ругается на структуру.
      + '<w:p/>'
    );
  }

  function documentXml(doc) {
    const parts = [];
    parts.push(paragraphXml(doc.title, { style: 'Title', align: 'center', bold: true, size: 32 }));
    if (doc.subtitle) {
      parts.push(paragraphXml(doc.subtitle, { style: 'Subtitle', align: 'center', size: 24 }));
    }

    (doc.clauses || []).forEach((clause, index) => {
      if (clause.title) {
        parts.push(
          paragraphXml(index + 1 + '. ' + clause.title, {
            style: 'Heading1',
            align: 'left',
            bold: true,
            size: 28,
          })
        );
      }
      (clause.paragraphs || []).forEach((text, position) => {
        if (clause.kind === 'list') {
          parts.push(paragraphXml('• ' + text, { indent: 567, hanging: 283, align: 'left' }));
        } else if (clause.kind === 'ordered') {
          parts.push(paragraphXml(position + 1 + '. ' + text, { indent: 567, hanging: 283, align: 'left' }));
        } else {
          parts.push(paragraphXml(text, { indent: 0 }));
        }
      });
      if ((clause.rows || []).length) parts.push(tableXml(clause.rows));
    });

    if (doc.legalBasis) {
      parts.push(paragraphXml(doc.legalBasis, { italic: true, size: 20, align: 'left' }));
    }

    return (
      XML_HEAD
      + '<w:document xmlns:w="' + W_NS + '"><w:body>'
      + parts.join('')
      + '<w:sectPr><w:pgSz w:w="' + A4_WIDTH + '" w:h="16838"/>'
      + '<w:pgMar w:top="1134" w:right="' + MARGIN_RIGHT + '" w:bottom="1134" w:left="' + MARGIN_LEFT + '" '
      + 'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
      + '</w:body></w:document>'
    );
  }

  function toDocx(doc) {
    return zipStore([
      { name: '[Content_Types].xml', data: utf8(CONTENT_TYPES) },
      { name: '_rels/.rels', data: utf8(ROOT_RELS) },
      { name: 'word/_rels/document.xml.rels', data: utf8(DOCUMENT_RELS) },
      { name: 'word/styles.xml', data: utf8(STYLES_XML) },
      { name: 'word/document.xml', data: utf8(documentXml(doc)) },
    ]);
  }

  // -------------------------------------------------------------------- .html

  const PRINT_CSS = [
    '@page{size:A4;margin:20mm 15mm 20mm 25mm}',
    'html{-webkit-text-size-adjust:100%}',
    'body{font:12pt/1.55 Georgia,"Times New Roman",serif;color:#111;background:#fff;',
    'max-width:190mm;margin:0 auto;padding:24px;text-align:justify}',
    'h1{font-size:18pt;text-align:center;margin:0 0 4px}',
    '.subtitle{text-align:center;font-size:12pt;color:#444;margin:0 0 28px}',
    'h2{font-size:13pt;margin:22px 0 8px;text-align:left;page-break-after:avoid}',
    'p{margin:0 0 10px}',
    'ul,ol{margin:0 0 10px;padding-left:22px}',
    'li{margin:0 0 6px}',
    'table{border-collapse:collapse;width:100%;margin:0 0 12px;font-size:11pt}',
    'th,td{border:1px solid #999;padding:6px 8px;text-align:left;vertical-align:top}',
    'th{background:#f2f2f2}',
    '.basis{font-size:10pt;color:#555;font-style:italic;margin-top:24px}',
    '@media print{body{max-width:none;padding:0}}',
  ].join('');

  function listHtml(clause) {
    const tag = clause.kind === 'ordered' ? 'ol' : 'ul';
    const items = (clause.paragraphs || []).map((text) => '<li>' + escapeHtml(text) + '</li>').join('');
    return '<' + tag + '>' + items + '</' + tag + '>';
  }

  function tableHtml(rows) {
    const useHead = rows.length > 1;
    const head = useHead
      ? '<thead><tr>' + rows[0].map((cell) => '<th>' + escapeHtml(cell) + '</th>').join('') + '</tr></thead>'
      : '';
    const body = rows
      .slice(useHead ? 1 : 0)
      .map((row) => '<tr>' + row.map((cell) => '<td>' + escapeHtml(cell) + '</td>').join('') + '</tr>')
      .join('');
    return '<table>' + head + '<tbody>' + body + '</tbody></table>';
  }

  function toHtml(doc) {
    const parts = [];
    parts.push('<!doctype html><html lang="ru"><head><meta charset="utf-8">');
    parts.push('<meta name="viewport" content="width=device-width, initial-scale=1">');
    parts.push('<meta name="robots" content="noindex">');
    parts.push('<title>' + escapeHtml(doc.title) + '</title>');
    parts.push('<style>' + PRINT_CSS + '</style></head><body>');
    parts.push('<h1>' + escapeHtml(doc.title) + '</h1>');
    if (doc.subtitle) parts.push('<p class="subtitle">' + escapeHtml(doc.subtitle) + '</p>');

    (doc.clauses || []).forEach((clause, index) => {
      if (clause.title) {
        parts.push('<h2>' + escapeHtml(index + 1 + '. ' + clause.title) + '</h2>');
      }
      const paragraphs = clause.paragraphs || [];
      if (paragraphs.length && (clause.kind === 'list' || clause.kind === 'ordered')) {
        parts.push(listHtml(clause));
      } else {
        paragraphs.forEach((text) => parts.push('<p>' + escapeHtml(text) + '</p>'));
      }
      if ((clause.rows || []).length) parts.push(tableHtml(clause.rows));
    });

    if (doc.legalBasis) parts.push('<p class="basis">' + escapeHtml(doc.legalBasis) + '</p>');
    parts.push('</body></html>');
    return parts.join('');
  }

  // ------------------------------------------------------------- текст и файлы

  /** Точная копия ``RenderedDocument.plain_text`` из schema.py. */
  function toText(doc) {
    const lines = [doc.title];
    if (doc.subtitle) lines.push(doc.subtitle);
    lines.push('');
    (doc.clauses || []).forEach((clause, index) => {
      if (clause.title) lines.push(index + 1 + '. ' + clause.title);
      (clause.paragraphs || []).forEach((text) => lines.push(text));
      (clause.rows || []).forEach((row) => lines.push(row.join(' | ')));
      lines.push('');
    });
    return lines.join('\n').trim();
  }

  function baseName(doc) {
    let name = String((doc && doc.filename) || (doc && doc.code) || 'dokument').trim();
    name = name.replace(/\.(docx|doc|html|htm|txt|pdf|odt)$/i, '');
    // RU: Символы, запрещённые в именах файлов Windows, плюс управляющие.
    name = name.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '-').replace(/\s+/g, '-');
    return name.replace(/-{2,}/g, '-').replace(/^-|-$/g, '') || 'dokument';
  }

  function fileName(doc, extension) {
    return baseName(doc) + '.' + String(extension || 'txt');
  }

  function docxBlob(doc) {
    return new Blob([toDocx(doc)], {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
  }

  function htmlBlob(doc) {
    return new Blob([toHtml(doc)], { type: 'text/html;charset=utf-8' });
  }

  function textBlob(doc) {
    return new Blob([toText(doc)], { type: 'text/plain;charset=utf-8' });
  }

  function download(filename, blob) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = String(filename || 'dokument');
    link.rel = 'noopener';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    // RU: Отзываем ссылку не сразу — иначе Safari не успевает начать скачивание.
    window.setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  /** Печать через скрытый iframe: не блокируется как всплывающее окно. */
  function printDoc(doc) {
    const html = toHtml(doc);
    const frame = document.createElement('iframe');
    if (!('srcdoc' in frame)) {
      const view = window.open('', '_blank');
      if (!view) return;
      view.document.open();
      view.document.write(html);
      view.document.close();
      view.focus();
      view.print();
      return;
    }
    frame.setAttribute('aria-hidden', 'true');
    frame.style.position = 'fixed';
    frame.style.right = '0';
    frame.style.bottom = '0';
    frame.style.width = '0';
    frame.style.height = '0';
    frame.style.border = '0';
    frame.srcdoc = html;
    frame.onload = function () {
      try {
        frame.contentWindow.focus();
        frame.contentWindow.print();
      } catch (error) {
        // RU: Печать могла быть запрещена — страницу это ронять не должно.
      }
      window.setTimeout(() => {
        if (frame.parentNode) frame.parentNode.removeChild(frame);
      }, 60000);
    };
    document.body.appendChild(frame);
  }

  D.docgen = {
    toHtml: toHtml,
    toDocx: toDocx,
    toText: toText,
    printDoc: printDoc,
    download: download,
    docxBlob: docxBlob,
    htmlBlob: htmlBlob,
    textBlob: textBlob,
    fileName: fileName,
    baseName: baseName,
    zipStore: zipStore,
    crc32: crc32,
    utf8: utf8,
    escapeXml: escapeXml,
  };
})();
