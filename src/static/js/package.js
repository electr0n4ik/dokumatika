/**
 * Страница оплаченного заказа: сборка платного комплекта в браузере.
 *
 * Сервер знает только факт оплаты. Ответы визарда он не видел и не увидит:
 * документы собираются здесь из localStorage теми же правилами, что и
 * бесплатная политика. Отсюда и главный краевой случай — человек оплатил в
 * одном браузере, а ссылку открыл в другом. Тогда честно объясняем, что данных
 * нет, и отправляем заполнить ответы: доступ к заказу от этого не пропадает.
 */
(function () {
  'use strict';

  const D = window.Dokumatika || {};

  const root = document.getElementById('package-app');
  if (!root) return;

  const el = D.el;
  const clear = D.clear;

  function fail(message) {
    const node = document.createElement('p');
    node.className = 'pkg-fail';
    node.textContent = message;
    clear ? clear(root) : (root.textContent = '');
    root.appendChild(node);
  }

  if (!el || !D.loadData || !D.docgen) {
    fail('Не удалось загрузить сборщик документов. Обновите страницу.');
    return;
  }

  const docgen = D.docgen;
  const state = { data: null, answers: {}, values: {}, docs: [] };

  // ---------------------------------------------------------------- состояния

  function emptyState() {
    clear(root);
    const box = el('div', { class: 'callout callout-warn pkg-empty' }, [
      el('h3', { text: 'Ответы не найдены в этом браузере' }),
      el('p', {
        text: 'Ваши ответы никогда не отправлялись на сервер — они хранятся только на устройстве. '
          + 'Похоже, эта страница открыта в другом браузере или данные сайта были очищены.',
      }),
      el('p', {
        text: 'Заполните ответы на главной странице (это пара минут) и вернитесь сюда по этой же '
          + 'ссылке — она постоянная и продублирована в письме о заказе.',
      }),
      el('a', { class: 'btn btn-p', text: 'Заполнить ответы', attrs: { href: '/' } }),
    ]);
    root.appendChild(box);
  }

  function missingKeys() {
    const keys = {};
    state.docs.forEach((doc) => {
      const text = docgen.toText(doc);
      const found = text.match(/\[не заполнено: [a-z0-9_]+\]/g) || [];
      found.forEach((item) => {
        keys[item.replace('[не заполнено: ', '').replace(']', '')] = true;
      });
    });
    return Object.keys(keys);
  }

  /** Ключ плейсхолдера -> человеческий вопрос: напрямую или через правило значения. */
  function keyTitle(key) {
    const questions = state.data.questions || [];
    const direct = questions.find((item) => item.id === key);
    if (direct) return direct.title;
    const rule = (state.data.valueRules || []).find((item) => item.key === key);
    const source = rule && rule.field ? questions.find((item) => item.id === rule.field) : null;
    return source ? source.title : key;
  }

  function missingNode(keys) {
    const list = el('ul', { class: 'pkg-missing-list' });
    const seen = {};
    keys.forEach((key) => {
      const title = keyTitle(key);
      if (seen[title]) return;
      seen[title] = true;
      list.appendChild(el('li', { text: title }));
    });
    return el('div', { class: 'callout callout-warn pkg-missing' }, [
      el('h3', { text: 'В документах есть незаполненные поля' }),
      el('p', {
        text: 'Эти данные не были указаны в визарде — в тексте они помечены как «не заполнено». '
          + 'Заполните их на главной и вернитесь сюда: документы пересоберутся сами.',
      }),
      list,
      el('a', { class: 'btn btn-s', text: 'Дополнить ответы', attrs: { href: '/' } }),
    ]);
  }

  // ------------------------------------------------------------------ карточки

  function actionsNode(doc) {
    const box = el('div', { class: 'pkg-actions' }, [
      el('button', {
        class: 'btn btn-p',
        text: 'Скачать .docx',
        attrs: { type: 'button', 'data-doc-action': 'docx', 'data-code': doc.code },
      }),
      el('button', {
        class: 'btn btn-s',
        text: 'Скачать .html',
        attrs: { type: 'button', 'data-doc-action': 'html', 'data-code': doc.code },
      }),
      el('button', {
        class: 'btn btn-s',
        text: 'Печать или PDF',
        attrs: { type: 'button', 'data-doc-action': 'print', 'data-code': doc.code },
      }),
      el('button', {
        class: 'btn btn-s',
        text: 'Скопировать текст',
        attrs: { type: 'button', 'data-doc-action': 'copy', 'data-code': doc.code },
      }),
    ]);
    box.addEventListener('click', onDocAction);
    return box;
  }

  function documentCard(doc, template) {
    const card = el('article', { class: 'pkg-doc' });
    card.appendChild(el('h3', { class: 'pkg-doc-title', text: doc.title }));
    if (template.purpose) card.appendChild(el('p', { class: 'muted', text: template.purpose }));
    card.appendChild(actionsNode(doc));

    (template.notes || []).forEach((note) => {
      card.appendChild(el('p', { class: 'pkg-note', text: D.fillPlaceholders(note, state.values) }));
    });

    const preview = el('details', { class: 'pkg-preview' }, [
      el('summary', { text: 'Посмотреть текст документа' }),
    ]);
    preview.appendChild(D.renderDocumentNode(doc));
    card.appendChild(preview);
    return card;
  }

  function docByCode(code) {
    return state.docs.find((doc) => doc.code === code) || null;
  }

  function onDocAction(event) {
    const button = event.target.closest ? event.target.closest('[data-doc-action]') : null;
    if (!button) return;
    const doc = docByCode(button.getAttribute('data-code'));
    if (!doc) return;
    const action = button.getAttribute('data-doc-action');

    if (action === 'docx') {
      docgen.download(docgen.fileName(doc, 'docx'), docgen.docxBlob(doc));
    } else if (action === 'html') {
      docgen.download(docgen.fileName(doc, 'html'), docgen.htmlBlob(doc));
    } else if (action === 'print') {
      docgen.printDoc(doc);
    } else if (action === 'copy') {
      const label = button.textContent;
      D.copyText(docgen.toText(doc)).then((ok) => {
        button.textContent = ok ? 'Скопировано' : 'Не получилось скопировать';
        window.setTimeout(() => {
          button.textContent = label;
        }, 2000);
      });
      D.track('package_download', 'copy');
      return;
    }
    D.track('package_download', doc.code || action);
  }

  // -------------------------------------------------------------------- архив

  function readmeText(names) {
    const lines = [
      'Комплект документов по 152-ФЗ',
      'Сформирован: ' + D.todayLabel(new Date()),
      '',
      'В архиве:',
    ];
    names.forEach((name, index) => lines.push(index + 1 + '. ' + name));
    lines.push('');
    lines.push('Документы типовые и собраны автоматически по вашим ответам.');
    lines.push('Это не юридическая консультация: перед публикацией прочитайте текст');
    lines.push('и при необходимости поправьте под свои процессы.');
    lines.push('');
    lines.push('Уведомление в Роскомнадзор подаётся до начала обработки данных');
    lines.push('(ч. 1 ст. 22 152-ФЗ) — памятка со списком полей лежит в этом же архиве.');
    return lines.join('\n');
  }

  function downloadArchive() {
    const used = {};
    const files = [];
    const names = [];

    state.docs.forEach((doc, index) => {
      let name = docgen.fileName(doc, 'docx');
      // RU: Совпадение имён в архиве превращает часть файлов в невидимки.
      if (used[name]) name = docgen.baseName(doc) + '-' + (index + 1) + '.docx';
      used[name] = true;
      files.push({ name: name, data: docgen.toDocx(doc) });
      names.push(doc.title + ' — ' + name);
    });
    files.push({ name: 'README.txt', data: docgen.utf8(readmeText(names)) });

    const blob = new Blob([docgen.zipStore(files)], { type: 'application/zip' });
    docgen.download('dokumatika-komplekt-152-fz.zip', blob);
    D.track('package_download', 'zip');
  }

  function archiveNode() {
    const button = el('button', {
      class: 'btn btn-p btn-wide',
      text: 'Скачать всё архивом (.zip)',
      attrs: { type: 'button' },
    });
    button.addEventListener('click', downloadArchive);
    return el('div', { class: 'pkg-archive' }, [
      button,
      el('p', {
        class: 'muted',
        text: 'В архиве все документы комплекта в формате .docx и короткая памятка. '
          + 'Файлы собираются в браузере, поэтому скачивание не зависит от нашего сервера.',
      }),
    ]);
  }

  // -------------------------------------------------------------------- сборка

  function build() {
    const templates = (state.data.paid || [])
      .map((code) => state.data.byCode[code])
      .filter((template) => Boolean(template));

    state.answers = D.visibleAnswers(D.loadAnswers(), state.data.questions);
    state.values = D.computeValues(state.answers, state.data.valueRules, state.data.questions);
    state.docs = templates.map((template) => D.renderDocument(template, state.answers, state.values));

    clear(root);
    const shell = el('div', { class: 'pkg' });
    shell.appendChild(
      el('p', {
        class: 'pkg-privacy',
        text: 'Документы собраны прямо в этом браузере из ваших ответов — на сервер они не отправлялись.',
      })
    );

    const missing = missingKeys();
    if (missing.length) shell.appendChild(missingNode(missing));

    shell.appendChild(archiveNode());
    const list = el('div', { class: 'pkg-list' });
    state.docs.forEach((doc, index) => list.appendChild(documentCard(doc, templates[index])));
    shell.appendChild(list);
    shell.appendChild(
      el('aside', { class: 'legalnote', attrs: { role: 'note' } }, [
        el('strong', { text: 'Важно. ' }),
        'Комплект типовой и сформирован автоматически по вашим ответам. Это не юридическая консультация.',
      ])
    );
    root.appendChild(shell);
  }

  function start() {
    const answers = D.loadAnswers();
    if (!Object.keys(answers).length) {
      emptyState();
      return;
    }
    // RU: Платные шаблоны отдаются только по токену оплаченного заказа —
    // он проставлен сервером в data-атрибуте контейнера.
    const token = (el && el.dataset && el.dataset.orderToken) || '';
    const source = token ? '/api/package.json?token=' + encodeURIComponent(token) : undefined;
    D.loadData(source)
      .then((data) => {
        state.data = data;
        build();
      })
      .catch(() => fail('Не удалось загрузить шаблоны. Проверьте соединение и обновите страницу.'));
  }

  if (D.onReady) {
    D.onReady(start);
  } else {
    start();
  }
})();
