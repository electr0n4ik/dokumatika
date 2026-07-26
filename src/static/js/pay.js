/**
 * Автоотправка формы на платёжную страницу Robokassa.
 *
 * Скрипт вынесен в отдельный файл намеренно. Инлайновый <script> потребовал бы
 * 'unsafe-inline' в script-src, то есть снятия основной защиты от XSS на всём
 * сайте — ради трёх строк на одной странице. Внешний файл проходит строгий
 * CSP `script-src 'self'`.
 *
 * Если JS выключен, форма остаётся на странице с обычной кнопкой отправки —
 * пользователь просто нажмёт её сам.
 */
(function () {
  'use strict';

  function submitCheckout() {
    var form = document.getElementById('robokassa-form');
    if (!form) return;

    // RU: Повторная отправка создала бы вторую попытку оплаты по тому же InvId
    // и упёрлась бы в ошибку 40 у Robokassa.
    if (form.dataset.submitted === '1') return;
    form.dataset.submitted = '1';

    var button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.textContent = 'Открываем страницу оплаты…';
    }

    form.submit();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', submitCheckout);
  } else {
    submitCheckout();
  }
})();
