/**
 * Визард политики конфиденциальности: вопросы, предпросмотр, чек-лист.
 *
 * Главное правило проекта: ответы пользователя не уходят на сервер. Всё
 * считается здесь, в браузере, и хранится в localStorage. На сервер летят
 * только обезличенные события воронки — без единого значения из формы.
 *
 * Движок условий и плейсхолдеров — построчный порт Python-версии
 * (src/app/documents/schema.py и src/app/documents/wizard.py). Расхождение
 * означало бы разный документ на витрине и у пользователя, поэтому правила
 * намеренно примитивны и переносятся один в один.
 */
(function () {
  'use strict';

  const D = (window.Dokumatika = window.Dokumatika || {});

  const ANSWERS_KEY = 'dokumatika.answers.v1';
  const DATA_URL = '/api/wizard.json';
  const TRACK_URL = '/api/track';
  const PLACEHOLDER_RE = /\{\{([a-z0-9_]+)\}\}/g;

  // ------------------------------------------------------------------ движок

  /** Питоновская истинность: пустой список и пустая строка — ложь. */
  function isTruthy(value) {
    if (value === null || value === undefined || value === false) return false;
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === 'string') return value.length > 0;
    if (typeof value === 'number') return value !== 0;
    return true;
  }

  function normalize(value) {
    return value === undefined ? null : value;
  }

  function sameValue(left, right) {
    if (Array.isArray(left) && Array.isArray(right)) {
      return left.length === right.length && left.every((item, index) => sameValue(item, right[index]));
    }
    return left === right;
  }

  /** Аналог питоновского ``item in container`` для списка и строки. */
  function membership(container, item) {
    if (Array.isArray(container)) return container.some((entry) => sameValue(entry, item));
    if (typeof container === 'string') return typeof item === 'string' && container.indexOf(item) !== -1;
    return false;
  }

  function evaluateCondition(condition, answers) {
    if (!condition || typeof condition !== 'object') return false;
    const actual = normalize((answers || {})[condition.field]);
    const value = normalize(condition.value);

    switch (condition.op) {
      case 'truthy':
        return isTruthy(actual);
      case 'falsy':
        return !isTruthy(actual);
      case 'eq':
        return sameValue(actual, value);
      case 'ne':
        return !sameValue(actual, value);
      case 'in':
        return membership(value, actual);
      case 'not_in':
        return !membership(value, actual);
      case 'contains':
        return membership(actual, value);
      case 'not_contains':
        return !membership(actual, value);
      default:
        // RU: Незнакомая операция — условие не выполнено. Так же в Python.
        return false;
    }
  }

  function evaluateConditions(conditions, answers) {
    if (!conditions || !conditions.length) return true;
    return conditions.every((condition) => evaluateCondition(condition, answers));
  }

  function pad2(value) {
    const text = String(value);
    return text.length >= 2 ? text : '0'.repeat(2 - text.length) + text;
  }

  /** ISO ``2026-07-26`` -> ``26.07.2026``. Всё остальное возвращаем как есть. */
  function formatDate(value) {
    const text = value === null || value === undefined ? '' : String(value);
    const parts = text.split('-');
    if (parts.length === 3 && parts.every((part) => /^[0-9]+$/.test(part))) {
      return pad2(parts[2]) + '.' + pad2(parts[1]) + '.' + parts[0];
    }
    return text;
  }

  function todayLabel(date) {
    const moment = date instanceof Date ? date : new Date();
    return pad2(moment.getDate()) + '.' + pad2(moment.getMonth() + 1) + '.' + moment.getFullYear();
  }

  /** Ответ в текст: список склеиваем, ложные значения считаем пустотой. */
  function asText(value) {
    if (!isTruthy(value)) return '';
    if (Array.isArray(value)) return value.join(', ');
    return String(value);
  }

  function optionLabels(questions, questionId) {
    const labels = {};
    const question = (questions || []).find((item) => item && item.id === questionId);
    if (question && Array.isArray(question.options)) {
      question.options.forEach((option) => {
        labels[String(option.value)] = String(option.label);
      });
    }
    return labels;
  }

  function computeValues(answers, valueRules, questions, today) {
    const source = answers || {};
    const rules = valueRules || (cachedData ? cachedData.valueRules : []) || [];
    const allQuestions = questions || (cachedData ? cachedData.questions : []) || [];
    const moment = today instanceof Date ? today : new Date();
    const values = {};

    rules.forEach((rule) => {
      if (!rule || !rule.key) return;
      const fallback = rule.fallback || '';

      if (rule.type === 'field') {
        const raw = source[rule.field];
        const text = rule.field === 'doc_date' ? formatDate(raw) : asText(raw);
        values[rule.key] = text.trim() || fallback;
      } else if (rule.type === 'map') {
        const mapping = rule.mapping || {};
        const key = asText(source[rule.field]);
        values[rule.key] = Object.prototype.hasOwnProperty.call(mapping, key) ? mapping[key] : fallback;
      } else if (rule.type === 'labels') {
        const labels = optionLabels(allQuestions, rule.field);
        const raw = source[rule.field];
        let selected = raw === null || raw === undefined || raw === false ? [] : raw;
        if (!Array.isArray(selected)) selected = [selected];
        const names = selected
          .map((item) => String(item))
          // RU: «Никому не передаю» — техническая отметка, в текст документа не идёт.
          .filter((item) => item !== 'none')
          .map((item) => (Object.prototype.hasOwnProperty.call(labels, item) ? labels[item] : item));
        values[rule.key] = names.length ? names.join(rule.separator || ', ') : fallback;
      } else if (rule.type === 'const') {
        values[rule.key] = rule.text || '';
      } else if (rule.type === 'today') {
        values[rule.key] = todayLabel(moment);
      } else if (rule.type === 'join') {
        const chunks = (rule.parts || []).map((part) => String(values[part] || '').trim());
        values[rule.key] = chunks.filter((chunk) => chunk).join(' ') || fallback;
      }
    });

    // RU: Дата документа по умолчанию — сегодня, иначе в шапке зияет пустота.
    if (!values.doc_date) values.doc_date = todayLabel(moment);
    return values;
  }

  function fillPlaceholders(text, values) {
    const source = values || {};
    return String(text === null || text === undefined ? '' : text).replace(PLACEHOLDER_RE, (match, key) => {
      const value = source[key];
      if (value === null || value === undefined || value === '') return '[не заполнено: ' + key + ']';
      if (Array.isArray(value)) return value.join(', ');
      return String(value);
    });
  }

  function renderDocument(template, answers, values) {
    const source = template || {};
    const clauses = (source.clauses || [])
      .filter((clause) => evaluateConditions(clause.when || [], answers || {}))
      .map((clause) => ({
        id: clause.id || '',
        title: fillPlaceholders(clause.title || '', values),
        paragraphs: (clause.paragraphs || []).map((item) => fillPlaceholders(item, values)),
        kind: clause.kind || 'text',
        rows: (clause.rows || []).map((row) => row.map((cell) => fillPlaceholders(cell, values))),
      }));
    return {
      code: source.code || '',
      title: fillPlaceholders(source.title || '', values),
      subtitle: fillPlaceholders(source.subtitle || '', values),
      filename: source.filename || source.code || 'dokument',
      legalBasis: source.legalBasis || '',
      clauses: clauses,
    };
  }

  /** Ответы на скрытые вопросы в документ не попадают: их как будто не задавали. */
  function visibleAnswers(answers, questions) {
    const source = answers || {};
    const result = {};
    (questions || []).forEach((question) => {
      if (!question || !question.id) return;
      if (!evaluateConditions(question.when || [], source)) return;
      if (source[question.id] === undefined) return;
      result[question.id] = source[question.id];
    });
    return result;
  }

  // ------------------------------------------------------- хранение и события

  function loadAnswers() {
    try {
      const raw = window.localStorage.getItem(ANSWERS_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
      return parsed;
    } catch (error) {
      // RU: Приватный режим может запретить хранилище — визард обязан работать и так.
      return {};
    }
  }

  function saveAnswers(answers) {
    try {
      window.localStorage.setItem(ANSWERS_KEY, JSON.stringify(answers || {}));
    } catch (error) {
      return;
    }
  }

  function clearAnswers() {
    try {
      window.localStorage.removeItem(ANSWERS_KEY);
    } catch (error) {
      return;
    }
  }

  function track(event, label) {
    if (typeof window.fetch !== 'function') return;
    try {
      window
        .fetch(TRACK_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ event: String(event || ''), label: String(label || '') }),
          keepalive: true,
        })
        .catch(() => {});
    } catch (error) {
      // RU: Аналитика никогда не должна ломать страницу.
      return;
    }
  }

  let cachedData = null;
  let dataPromise = null;

  function normalizeData(payload) {
    const wizard = (payload && payload.wizard) || {};
    const documents = (payload && payload.documents) || {};
    const templates = documents.templates || [];
    const byCode = {};
    templates.forEach((template) => {
      if (template && template.code) byCode[template.code] = template;
    });
    return {
      steps: wizard.steps || [],
      questions: wizard.questions || [],
      valueRules: wizard.valueRules || [],
      free: documents.free || [],
      paid: documents.paid || [],
      templates: templates,
      byCode: byCode,
    };
  }

  // RU: Кэш по адресу, а не один на всё приложение: публичный /api/wizard.json
  // отдаёт только бесплатный шаблон, а платный комплект приезжает с другого
  // адреса и только по токену оплаченного заказа.
  const dataCache = {};
  const dataPromises = {};

  function loadData(url) {
    const source = url || DATA_URL;
    if (dataCache[source]) return Promise.resolve(dataCache[source]);
    if (dataPromises[source]) return dataPromises[source];
    dataPromises[source] = window
      .fetch(source, { credentials: 'same-origin' })
      .then((response) => {
        if (!response.ok) throw new Error('bad status');
        return response.json();
      })
      .then((payload) => {
        dataCache[source] = normalizeData(payload);
        return dataCache[source];
      })
      .catch((error) => {
        dataPromises[source] = null;
        throw error;
      });
    return dataPromises[source];
  }

  // ------------------------------------------------------------ DOM-хелперы

  function el(tag, options, children) {
    const node = document.createElement(tag);
    const config = options || {};
    if (config.class) node.className = config.class;
    if (config.id) node.id = config.id;
    if (config.text !== undefined && config.text !== null) node.textContent = String(config.text);
    if (config.attrs) {
      Object.keys(config.attrs).forEach((name) => {
        const value = config.attrs[name];
        if (value === null || value === undefined || value === false) return;
        node.setAttribute(name, value === true ? '' : String(value));
      });
    }
    (children || []).forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function copyText(text) {
    const value = String(text || '');
    if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
      return window.navigator.clipboard.writeText(value).then(() => true).catch(() => fallbackCopy(value));
    }
    return Promise.resolve(fallbackCopy(value));
  }

  function fallbackCopy(value) {
    try {
      const area = el('textarea', { attrs: { readonly: true, 'aria-hidden': 'true' } });
      area.value = value;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(area);
      return Boolean(ok);
    } catch (error) {
      return false;
    }
  }

  /** Экранная версия документа. Весь текст — через textContent, без innerHTML. */
  function renderDocumentNode(doc) {
    const article = el('article', { class: 'doc' });
    article.appendChild(el('h3', { class: 'doc-title', text: doc.title }));
    if (doc.subtitle) article.appendChild(el('p', { class: 'doc-subtitle', text: doc.subtitle }));

    (doc.clauses || []).forEach((clause, index) => {
      const block = el('section', { class: 'doc-clause' });
      if (clause.title) {
        block.appendChild(el('h4', { class: 'doc-clause-title', text: index + 1 + '. ' + clause.title }));
      }
      const paragraphs = clause.paragraphs || [];
      if (paragraphs.length && (clause.kind === 'list' || clause.kind === 'ordered')) {
        const list = el(clause.kind === 'ordered' ? 'ol' : 'ul', { class: 'doc-list' });
        paragraphs.forEach((item) => list.appendChild(el('li', { text: item })));
        block.appendChild(list);
      } else {
        paragraphs.forEach((item) => block.appendChild(el('p', { text: item })));
      }
      if ((clause.rows || []).length) block.appendChild(tableNode(clause.rows));
      article.appendChild(block);
    });

    if (doc.legalBasis) article.appendChild(el('p', { class: 'doc-basis', text: doc.legalBasis }));
    return article;
  }

  /** Первая строка таблицы считается шапкой, если строк больше одной. */
  function tableNode(rows) {
    const table = el('table', { class: 'cmp doc-table' });
    const useHead = rows.length > 1;
    if (useHead) {
      const head = el('thead');
      const row = el('tr');
      rows[0].forEach((cell) => row.appendChild(el('th', { text: cell, attrs: { scope: 'col' } })));
      head.appendChild(row);
      table.appendChild(head);
    }
    const body = el('tbody');
    rows.slice(useHead ? 1 : 0).forEach((cells) => {
      const row = el('tr');
      cells.forEach((cell) => row.appendChild(el('td', { text: cell })));
      body.appendChild(row);
    });
    table.appendChild(body);
    return el('div', { class: 'table-wrap' }, [table]);
  }

  // ----------------------------------------------------------------- экспорт

  D.isTruthy = isTruthy;
  D.evaluateCondition = evaluateCondition;
  D.evaluateConditions = evaluateConditions;
  D.computeValues = computeValues;
  D.fillPlaceholders = fillPlaceholders;
  D.renderDocument = renderDocument;
  D.visibleAnswers = visibleAnswers;
  D.formatDate = formatDate;
  D.todayLabel = todayLabel;
  D.loadData = loadData;
  D.loadAnswers = loadAnswers;
  D.saveAnswers = saveAnswers;
  D.clearAnswers = clearAnswers;
  D.track = track;
  D.el = el;
  D.clear = clear;
  D.copyText = copyText;
  D.renderDocumentNode = renderDocumentNode;
  D.answersKey = ANSWERS_KEY;

  // ---------------------------------------------------------- состояние UI

  const state = {
    root: null,
    data: null,
    answers: {},
    step: 1,
    generated: false,
    started: false,
    completed: false,
    form: null,
    heading: null,
    questionsBox: null,
    formError: null,
    resultBox: null,
    previewBox: null,
    previewTimer: 0,
  };

  const CHECKLIST = [
    [
      'Отдельное согласие на обработку персональных данных',
      'С 01.09.2025 согласие оформляется самостоятельным документом: пунктом в политике, '
        + 'оферте или пользовательском соглашении его больше не заменить.',
    ],
    [
      'Отдельное согласие на рекламную рассылку',
      'Нужно, если вы отправляете письма и сообщения: ст. 15 152-ФЗ и ч. 1 ст. 18 закона «О рекламе».',
    ],
    [
      'Политика в отношении файлов cookie',
      'Нужна, если на сайте стоят счётчики аналитики или другие cookie.',
    ],
    [
      'Приказ о назначении ответственного за организацию обработки ПД',
      'Требование п. 1 ч. 1 ст. 18.1 152-ФЗ. Обычно ответственный — сам руководитель или ИП.',
    ],
    [
      'Уведомление в Роскомнадзор',
      'Подаётся до начала обработки (ч. 1 ст. 22 152-ФЗ). Прежние исключения для сайтов '
        + 'утратили силу с 01.09.2022.',
    ],
  ];

  function questionById(id) {
    return (state.data.questions || []).find((question) => question.id === id) || null;
  }

  function stepQuestions(step) {
    return (state.data.questions || []).filter((question) => Number(question.step) === Number(step));
  }

  function isVisible(question) {
    return evaluateConditions(question.when || [], state.answers);
  }

  function stepByIndex(index) {
    return (state.data.steps || []).find((step) => Number(step.index) === Number(index)) || null;
  }

  function lastStepIndex() {
    const steps = state.data.steps || [];
    return steps.length ? Number(steps[steps.length - 1].index) : 1;
  }

  function policyTemplate() {
    const code = (state.data.free || [])[0];
    return code ? state.data.byCode[code] || null : null;
  }

  // ------------------------------------------------------------- валидация

  const EMAIL_RE = /^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$/;

  function hasAnswer(question) {
    const value = state.answers[question.id];
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === 'string') return value.trim().length > 0;
    return value !== null && value !== undefined && value !== '';
  }

  function requiredMessage(question) {
    if (question.kind === 'checkbox') return 'Отметьте хотя бы один вариант.';
    if (question.kind === 'radio' || question.kind === 'bool') return 'Выберите один из вариантов.';
    if (question.kind === 'date') return 'Укажите дату.';
    return 'Заполните это поле.';
  }

  /** Мягкие проверки формата: срабатывают только на заполненном поле. */
  function formatMessage(question) {
    const value = String(state.answers[question.id] || '').trim();
    if (!value) return '';
    if (question.id === 'contact_email') {
      return EMAIL_RE.test(value) ? '' : 'Похоже на опечатку. Пример: privacy@example.ru';
    }
    if (question.id === 'inn') {
      return /^[0-9]{10}$|^[0-9]{12}$/.test(value)
        ? ''
        : 'ИНН — это 10 цифр у организации и 12 у ИП, самозанятого и физлица.';
    }
    if (question.id === 'ogrn') {
      return /^[0-9]{13}$|^[0-9]{15}$/.test(value) ? '' : 'ОГРН состоит из 13 цифр, ОГРНИП — из 15.';
    }
    if (question.id === 'site_url') {
      return /^https?:\/\/[^\s/]+\.[^\s/]{2,}/.test(value)
        ? ''
        : 'Укажите адрес целиком, вместе с https:// — например, https://example.ru';
    }
    return '';
  }

  function questionProblem(question) {
    if (question.required && !hasAnswer(question)) return requiredMessage(question);
    return formatMessage(question);
  }

  function validateStep(step) {
    const problems = [];
    stepQuestions(step).forEach((question) => {
      if (!isVisible(question)) return;
      const message = questionProblem(question);
      if (message) problems.push({ id: question.id, message: message });
    });
    return problems;
  }

  /** Адрес без схемы — частая ошибка; чиним сами, а не ругаемся. */
  function fixSiteUrl() {
    const value = String(state.answers.site_url || '').trim();
    if (!value || /^https?:\/\//i.test(value)) return;
    if (!/^[^\s/]+\.[^\s/]{2,}/.test(value)) return;
    state.answers.site_url = 'https://' + value;
    saveAnswers(state.answers);
    const field = document.getElementById('wz-f-site_url');
    if (field) field.value = state.answers.site_url;
  }

  // -------------------------------------------------------------- отрисовка

  function privacyNote() {
    return el('p', {
      class: 'wz-privacy',
      text: 'Ответы остаются в вашем браузере: документ собирается на устройстве, '
        + 'на сервер не уходит ни одно значение из формы.',
    });
  }

  function stepsNode() {
    const list = el('ol', { class: 'wz-steps' });
    (state.data.steps || []).forEach((step) => {
      const index = Number(step.index);
      const current = index === state.step;
      const button = el(
        'button',
        {
          class: 'wz-stepbtn',
          attrs: {
            type: 'button',
            'data-step': index,
            'aria-current': current ? 'step' : false,
          },
        },
        [
          el('span', { class: 'wz-stepnum', text: String(index) }),
          el('span', { class: 'wz-steptitle', text: step.title }),
        ]
      );
      const modifier = (current ? ' is-current' : '') + (index < state.step ? ' is-done' : '');
      list.appendChild(el('li', { class: 'wz-step' + modifier }, [button]));
    });
    list.addEventListener('click', onStepClick);
    return list;
  }

  function progressNode() {
    const total = (state.data.steps || []).length || 1;
    const done = state.generated ? total : state.step;
    const bar = el('span', { class: 'wz-bar' });
    bar.style.width = Math.round((done / total) * 100) + '%';
    return el(
      'div',
      {
        class: 'wz-progress',
        attrs: {
          role: 'progressbar',
          'aria-valuemin': '1',
          'aria-valuemax': String(total),
          'aria-valuenow': String(done),
          'aria-label': 'Прогресс заполнения',
        },
      },
      [bar]
    );
  }

  function optionsNode(question) {
    const box = el('div', { class: 'wz-options wz-options-' + question.kind });
    const options =
      question.kind === 'bool'
        ? [{ value: 'true', label: 'Да' }, { value: 'false', label: 'Нет' }]
        : question.options || [];
    const answer = state.answers[question.id];

    options.forEach((option, index) => {
      const inputId = 'wz-f-' + question.id + '-' + index;
      const input = el('input', {
        id: inputId,
        attrs: {
          type: question.kind === 'checkbox' ? 'checkbox' : 'radio',
          name: 'wz-' + question.id,
          value: option.value,
        },
      });
      if (question.kind === 'checkbox') {
        input.checked = Array.isArray(answer) && answer.indexOf(option.value) !== -1;
      } else if (question.kind === 'bool') {
        input.checked = (answer === true && option.value === 'true')
          || (answer === false && option.value === 'false');
      } else {
        input.checked = answer === option.value;
      }
      const body = el('span', { class: 'wz-optbody' }, [
        el('span', { class: 'wz-opttitle', text: option.label }),
        option.hint ? el('span', { class: 'wz-opthint', text: option.hint }) : null,
      ]);
      box.appendChild(el('label', { class: 'wz-option', attrs: { for: inputId } }, [input, body]));
    });
    return box;
  }

  function fieldNode(question, inputId) {
    const input = el('input', {
      id: inputId,
      class: 'wz-input',
      attrs: {
        type: question.kind === 'date' ? 'date' : 'text',
        name: 'wz-' + question.id,
        placeholder: question.placeholder || false,
        autocomplete: 'off',
        inputmode: question.id === 'inn' || question.id === 'ogrn' ? 'numeric' : false,
      },
    });
    input.value = String(state.answers[question.id] || '');
    return input;
  }

  function optionalBadge(question) {
    if (question.required) return null;
    return el('span', { class: 'wz-optional', text: 'необязательно' });
  }

  function questionNode(question) {
    const errorId = 'wz-err-' + question.id;
    const wrap = el('div', { class: 'wz-q', attrs: { 'data-qid': question.id } });
    const grouped = question.kind === 'radio' || question.kind === 'checkbox' || question.kind === 'bool';

    if (grouped) {
      const fieldset = el('fieldset', { class: 'wz-fieldset' });
      fieldset.appendChild(
        el('legend', { class: 'wz-legend' }, [question.title, ' ', optionalBadge(question)])
      );
      if (question.hint) fieldset.appendChild(el('p', { class: 'wz-hint', text: question.hint }));
      fieldset.appendChild(optionsNode(question));
      wrap.appendChild(fieldset);
    } else {
      const inputId = 'wz-f-' + question.id;
      wrap.appendChild(
        el('label', { class: 'wz-label', attrs: { for: inputId } }, [
          question.title,
          ' ',
          optionalBadge(question),
        ])
      );
      if (question.hint) wrap.appendChild(el('p', { class: 'wz-hint', text: question.hint }));
      wrap.appendChild(fieldNode(question, inputId));
    }

    wrap.appendChild(el('p', { class: 'wz-error', id: errorId, attrs: { role: 'alert', hidden: true } }));
    wrap.hidden = !isVisible(question);
    return wrap;
  }

  function formNode() {
    const step = stepByIndex(state.step);
    const total = (state.data.steps || []).length || 1;
    const form = el('form', { class: 'wz-form', attrs: { novalidate: true, autocomplete: 'off' } });

    const head = el('div', { class: 'wz-head' }, [
      el('span', { class: 'wz-count', text: 'Шаг ' + state.step + ' из ' + total }),
      el('h2', {
        class: 'wz-title',
        text: step ? step.title : 'Вопросы',
        attrs: { tabindex: '-1' },
      }),
      step && step.subtitle ? el('p', { class: 'wz-lead', text: step.subtitle }) : null,
    ]);
    state.heading = head.querySelector('.wz-title');

    const box = el('div', { class: 'wz-questions' });
    stepQuestions(state.step).forEach((question) => box.appendChild(questionNode(question)));
    box.addEventListener('change', onFieldChange);
    box.addEventListener('input', onFieldChange);
    state.questionsBox = box;

    const formError = el('p', { class: 'wz-formerror', attrs: { role: 'alert', hidden: true } });
    state.formError = formError;

    const isLast = state.step >= lastStepIndex();
    const nav = el('div', { class: 'wz-nav' }, [
      state.step > 1
        ? el('button', {
            class: 'btn btn-s',
            text: 'Назад',
            attrs: { type: 'button', 'data-action': 'back' },
          })
        : null,
      el('button', {
        class: 'btn btn-p',
        text: isLast ? 'Сформировать политику' : 'Далее',
        attrs: { type: 'submit' },
      }),
    ]);
    nav.addEventListener('click', onNavClick);

    form.appendChild(head);
    form.appendChild(box);
    form.appendChild(formError);
    form.appendChild(nav);
    form.addEventListener('submit', onSubmit);
    state.form = form;
    return form;
  }

  function renderApp(options) {
    const config = options || {};
    clear(state.root);
    const shell = el('div', { class: 'wizard' }, [privacyNote(), stepsNode(), progressNode(), formNode()]);
    const result = el('div', { class: 'wz-result', attrs: { hidden: true } });
    state.resultBox = result;
    shell.appendChild(result);
    state.root.appendChild(shell);
    if (state.generated) renderResult();
    if (config.focus && state.heading) {
      state.heading.focus({ preventScroll: true });
      shell.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // ---------------------------------------------------------------- события

  function onFieldChange(event) {
    const target = event.target;
    if (!target || !target.closest) return;
    const holder = target.closest('.wz-q');
    if (!holder) return;
    const question = questionById(holder.getAttribute('data-qid'));
    if (!question) return;

    state.answers[question.id] = readQuestion(question, holder);
    saveAnswers(state.answers);
    if (!state.started) {
      state.started = true;
      track('wizard_start', 'step-' + state.step);
    }
    hideError(holder);
    refreshVisibility();
    schedulePreview();
  }

  function readQuestion(question, holder) {
    if (question.kind === 'checkbox') {
      return Array.prototype.slice
        .call(holder.querySelectorAll('input[type="checkbox"]'))
        .filter((input) => input.checked)
        .map((input) => input.value);
    }
    if (question.kind === 'radio' || question.kind === 'bool') {
      const picked = holder.querySelector('input[type="radio"]:checked');
      if (!picked) return question.kind === 'bool' ? null : '';
      return question.kind === 'bool' ? picked.value === 'true' : picked.value;
    }
    const field = holder.querySelector('input');
    return field ? field.value : '';
  }

  function refreshVisibility() {
    if (!state.questionsBox) return;
    Array.prototype.slice.call(state.questionsBox.querySelectorAll('.wz-q')).forEach((node) => {
      const question = questionById(node.getAttribute('data-qid'));
      if (question) node.hidden = !isVisible(question);
    });
  }

  function hideError(holder) {
    const error = holder.querySelector('.wz-error');
    if (error) {
      error.textContent = '';
      error.hidden = true;
    }
    holder.classList.remove('is-invalid');
    if (state.formError) state.formError.hidden = true;
  }

  function showProblems(problems) {
    let first = null;
    (state.data.questions || []).forEach((question) => {
      const holder = state.questionsBox.querySelector('.wz-q[data-qid="' + question.id + '"]');
      if (holder) hideError(holder);
    });
    problems.forEach((problem) => {
      const holder = state.questionsBox.querySelector('.wz-q[data-qid="' + problem.id + '"]');
      if (!holder) return;
      const error = holder.querySelector('.wz-error');
      if (error) {
        error.textContent = problem.message;
        error.hidden = false;
      }
      holder.classList.add('is-invalid');
      if (!first) first = holder;
    });
    if (state.formError) {
      state.formError.textContent =
        problems.length === 1 ? 'Проверьте отмеченный вопрос.' : 'Проверьте отмеченные вопросы.';
      state.formError.hidden = false;
    }
    if (first) {
      const field = first.querySelector('input');
      if (field) field.focus({ preventScroll: true });
      first.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function onSubmit(event) {
    event.preventDefault();
    fixSiteUrl();
    const problems = validateStep(state.step);
    if (problems.length) {
      showProblems(problems);
      return;
    }
    if (state.step >= lastStepIndex()) {
      generate();
      return;
    }
    goToStep(state.step + 1);
  }

  function onNavClick(event) {
    const button = event.target.closest ? event.target.closest('[data-action]') : null;
    if (!button) return;
    if (button.getAttribute('data-action') === 'back') goToStep(state.step - 1);
  }

  function onStepClick(event) {
    const button = event.target.closest ? event.target.closest('[data-step]') : null;
    if (!button) return;
    const target = Number(button.getAttribute('data-step'));
    if (!target || target === state.step) return;
    if (target < state.step) {
      goToStep(target);
      return;
    }
    // RU: Вперёд по шагам — только через проверку всех промежуточных.
    for (let index = state.step; index < target; index += 1) {
      const problems = validateStep(index);
      if (problems.length) {
        if (index !== state.step) goToStep(index);
        showProblems(problems);
        return;
      }
    }
    goToStep(target);
  }

  function goToStep(step) {
    const target = Math.min(Math.max(1, step), lastStepIndex());
    const forward = target > state.step;
    state.step = target;
    renderApp({ focus: true });
    if (forward) track('wizard_step', 'step-' + target);
  }

  // ------------------------------------------------------------- результат

  function currentDocument() {
    const template = policyTemplate();
    if (!template) return null;
    const answers = visibleAnswers(state.answers, state.data.questions);
    const values = computeValues(answers, state.data.valueRules, state.data.questions);
    return renderDocument(template, answers, values);
  }

  function schedulePreview() {
    if (!state.generated || !state.previewBox) return;
    window.clearTimeout(state.previewTimer);
    state.previewTimer = window.setTimeout(() => {
      const doc = currentDocument();
      if (!doc || !state.previewBox) return;
      clear(state.previewBox);
      state.previewBox.appendChild(renderDocumentNode(doc));
    }, 250);
  }

  function docgen() {
    return D.docgen || null;
  }

  function actionsNode() {
    const box = el('div', { class: 'wz-actions' });
    const buttons = [
      ['docx', 'Скачать .docx', 'btn btn-p'],
      ['html', 'Скачать .html', 'btn btn-s'],
      ['print', 'Печать или PDF', 'btn btn-s'],
      ['copy', 'Скопировать текст', 'btn btn-s'],
    ];
    buttons.forEach((item) => {
      box.appendChild(
        el('button', {
          class: item[2],
          text: item[1],
          attrs: { type: 'button', 'data-download': item[0] },
        })
      );
    });
    box.addEventListener('click', onDownloadClick);
    return box;
  }

  function onDownloadClick(event) {
    const button = event.target.closest ? event.target.closest('[data-download]') : null;
    if (!button) return;
    const kind = button.getAttribute('data-download');
    const tools = docgen();
    const doc = currentDocument();
    if (!tools || !doc) return;

    if (kind === 'docx') {
      tools.download(tools.fileName(doc, 'docx'), tools.docxBlob(doc));
    } else if (kind === 'html') {
      tools.download(tools.fileName(doc, 'html'), tools.htmlBlob(doc));
    } else if (kind === 'print') {
      tools.printDoc(doc);
    } else if (kind === 'copy') {
      const label = button.textContent;
      copyText(tools.toText(doc)).then((ok) => {
        button.textContent = ok ? 'Скопировано' : 'Не получилось скопировать';
        window.setTimeout(() => {
          button.textContent = label;
        }, 2000);
      });
    }
    track('policy_download', kind);
  }

  function checklistNode() {
    const list = el('ul', { class: 'wz-checklist-list' });
    CHECKLIST.forEach((item) => {
      list.appendChild(
        el('li', {}, [el('strong', { text: item[0] }), el('span', { class: 'wz-cl-note', text: item[1] })])
      );
    });

    const price = state.root.getAttribute('data-price') || '';
    const cta = el('a', {
      class: 'btn btn-p btn-wide',
      text: price ? 'Собрать комплект — ' + price : 'Собрать комплект документов',
      attrs: { href: '/komplekt/', 'data-cta': 'komplekt' },
    });
    cta.addEventListener('click', () => track('checkout_click', 'checklist'));

    return el('div', { class: 'callout callout-info wz-checklist' }, [
      el('h3', { text: 'Политика готова. По 152-ФЗ вам ещё нужно:' }),
      list,
      el('p', {
        class: 'wz-cl-fine',
        text: 'Штраф за неподанное уведомление в Роскомнадзор (ч. 10 ст. 13.11 КоАП): '
          + '5 000–10 000 ₽ для граждан, 30 000–50 000 ₽ для ИП и должностных лиц, '
          + '100 000–300 000 ₽ для организаций.',
      }),
      cta,
    ]);
  }

  function publishHintNode() {
    const list = el('ul', { class: 'wz-todo' });
    [
      'Разместите политику текстом на отдельной странице сайта и не закрывайте её от поиска: '
        + 'ч. 2 ст. 18.1 152-ФЗ требует неограниченного доступа к документу.',
      'Поставьте ссылку на политику в подвале сайта и рядом с каждой формой.',
      'Проверьте реквизиты и адрес для обращений: на него будут приходить запросы об удалении '
        + 'данных и отзыве согласия.',
    ].forEach((text) => list.appendChild(el('li', { text: text })));
    return el('div', { class: 'wz-todo-box' }, [el('h3', { text: 'Что сделать с политикой' }), list]);
  }

  function resetNode() {
    const button = el('button', {
      class: 'wz-reset',
      text: 'Начать заново и удалить ответы',
      attrs: { type: 'button' },
    });
    button.addEventListener('click', () => {
      if (!window.confirm('Удалить сохранённые ответы и начать заново?')) return;
      clearAnswers();
      state.answers = {};
      state.step = 1;
      state.generated = false;
      renderApp({ focus: true });
    });
    return button;
  }

  function renderResult() {
    const doc = currentDocument();
    const box = state.resultBox;
    if (!box) return;
    clear(box);

    if (!doc) {
      const text = 'Шаблон политики не загрузился. Обновите страницу.';
      box.appendChild(el('p', { class: 'wz-fail', text: text }));
      box.hidden = false;
      return;
    }

    box.appendChild(el('h2', { text: 'Ваша политика конфиденциальности' }));
    box.appendChild(
      el('p', {
        class: 'muted',
        text: 'Прочитайте текст перед публикацией: он собран из ваших ответов и является типовым шаблоном, '
          + 'а не юридической консультацией.',
      })
    );
    box.appendChild(actionsNode());
    box.appendChild(
      el('p', { class: 'wz-hint', text: 'PDF получается из печати: в диалоге выберите «Сохранить в PDF».' })
    );

    // RU: Если страница сама объявила #doc-preview — рисуем внутрь него.
    const existing = document.getElementById('doc-preview');
    const preview = existing || el('div', { id: 'doc-preview', class: 'doc-preview' });
    clear(preview);
    preview.appendChild(renderDocumentNode(doc));
    state.previewBox = preview;
    if (!preview.parentNode) box.appendChild(preview);

    box.appendChild(publishHintNode());
    box.appendChild(checklistNode());
    box.appendChild(
      el('aside', { class: 'legalnote', attrs: { role: 'note' } }, [
        el('strong', { text: 'Важно. ' }),
        'Документ типовой и сформирован автоматически по вашим ответам. '
          + 'Это не юридическая консультация.',
      ])
    );
    box.appendChild(resetNode());
    box.hidden = false;
  }

  function generate() {
    state.generated = true;
    renderResult();
    if (!state.completed) {
      state.completed = true;
      track('wizard_complete', 'policy');
    }
    if (state.resultBox) state.resultBox.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ------------------------------------------------------------------ старт

  function restoreStep() {
    if (!Object.keys(state.answers).length) return 1;
    const steps = state.data.steps || [];
    for (let index = 0; index < steps.length; index += 1) {
      if (validateStep(steps[index].index).length) return Number(steps[index].index);
    }
    // RU: Все ответы на месте — человек вернулся к готовому документу.
    state.generated = true;
    return lastStepIndex();
  }

  function mount() {
    const root = document.getElementById('wizard-app');
    if (!root) return;
    loadData()
      .then((data) => {
        state.root = root;
        state.data = data;
        state.answers = loadAnswers();
        state.step = restoreStep();
        renderApp({ focus: false });
      })
      .catch(() => {
        // RU: Данные не пришли — серверную разметку не трогаем, она читаема без JS.
        root.appendChild(
          el('p', {
            class: 'wz-fail',
            text: 'Не удалось загрузить вопросы. Проверьте соединение и обновите страницу.',
          })
        );
      });
  }

  function onReady(callback) {
    if (document.readyState === 'complete') {
      callback();
      return;
    }
    let done = false;
    const run = function () {
      if (done) return;
      done = true;
      callback();
    };
    document.addEventListener('DOMContentLoaded', run, { once: true });
    window.addEventListener('load', run, { once: true });
  }

  D.onReady = onReady;
  onReady(mount);
})();
