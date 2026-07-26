/**
 * Оформление заказа на комплект документов.
 *
 * Форма на /komplekt/ размечена как обычная HTML-форма с method="post" и
 * action="/api/order", поэтому без JS она тоже отправится — просто сервер
 * ответит JSON вместо страницы. Этот скрипт перехватывает отправку, шлёт
 * запрос фоном и показывает понятную ошибку рядом с формой.
 *
 * Никаких данных визарда здесь нет и быть не может: на сервер уходят только
 * email и три галочки согласий.
 */
(function () {
  'use strict';

  var D = (window.Dokumatika = window.Dokumatika || {});

  // RU: Тексты ошибок сервера переводим на человеческий здесь, а не на бэкенде:
  // API остаётся машинным, а формулировки можно менять без правки Python.
  var MESSAGES = {
    bad_email: 'Проверьте адрес электронной почты — на него придут документы.',
    offer_required: 'Чтобы оформить заказ, нужно принять условия оферты.',
    privacy_required: 'Отметьте согласие на обработку персональных данных.',
    payments_disabled: 'Приём оплаты временно приостановлен. Попробуйте позже.',
    network: 'Не удалось связаться с сервером. Проверьте соединение и попробуйте ещё раз.',
    unknown: 'Не получилось оформить заказ. Попробуйте ещё раз или напишите нам.',
  };

  function setStatus(node, text, isError) {
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('is-error', Boolean(isError));
    node.classList.toggle('is-busy', !isError && Boolean(text));
  }

  function init() {
    var form = document.getElementById('order-form');
    if (!form) return;

    var status = document.getElementById('order-status');
    var submit = form.querySelector('button[type="submit"]');

    form.addEventListener('submit', function (event) {
      event.preventDefault();

      // RU: Повторная отправка создала бы второй заказ и второй InvId.
      if (form.dataset.busy === '1') return;

      var email = (form.querySelector('[name="email"]') || {}).value || '';
      var payload = {
        email: email.trim(),
        accept_privacy: isChecked(form, 'accept_privacy'),
        accept_offer: isChecked(form, 'accept_offer'),
        accept_marketing: isChecked(form, 'accept_marketing'),
      };

      // RU: Ту же проверку делает сервер; здесь она нужна лишь чтобы не гонять
      // запрос впустую и ответить мгновенно.
      if (!payload.accept_privacy) return setStatus(status, MESSAGES.privacy_required, true);
      if (!payload.accept_offer) return setStatus(status, MESSAGES.offer_required, true);

      form.dataset.busy = '1';
      if (submit) submit.disabled = true;
      setStatus(status, 'Оформляем заказ…', false);

      if (typeof D.track === 'function') D.track('checkout_click', 'komplekt');

      fetch('/api/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(function (response) {
          return response.json().then(
            function (data) {
              return { ok: response.ok, data: data };
            },
            function () {
              return { ok: false, data: {} };
            }
          );
        })
        .then(function (result) {
          if (result.ok && result.data && result.data.pay_url) {
            // RU: Дальше — страница-переходник, она сама отправит форму в Robokassa.
            window.location.href = result.data.pay_url;
            return;
          }
          var code = (result.data && result.data.error) || 'unknown';
          setStatus(status, MESSAGES[code] || (result.data && result.data.message) || MESSAGES.unknown, true);
          release();
        })
        .catch(function () {
          setStatus(status, MESSAGES.network, true);
          release();
        });

      function release() {
        form.dataset.busy = '';
        if (submit) submit.disabled = false;
      }
    });
  }

  function isChecked(form, name) {
    var node = form.querySelector('[name="' + name + '"]');
    return Boolean(node && node.checked);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
