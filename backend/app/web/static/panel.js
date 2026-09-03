/* Мелочи панели. Инлайновых обработчиков нет намеренно: CSP их запрещает. */
(function () {
  "use strict";

  // Подтверждение необратимых действий.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // Поля «скопируйте это»: выделяем содержимое целиком по клику.
  document.querySelectorAll(".copyfield").forEach(function (field) {
    field.addEventListener("focus", function () {
      field.select();
    });
    field.addEventListener("click", function () {
      field.select();
    });
  });

  // Кнопка «Копировать» рядом с полем.
  //
  // Clipboard API есть не всегда: при доступе по IP с самоподписанным
  // сертификатом браузер может не считать страницу «доверенным контекстом».
  // Поэтому запасной путь — старый execCommand по выделенному тексту.
  function copyText(field) {
    field.select();
    field.setSelectionRange(0, field.value.length);
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(field.value).then(
        function () {
          return true;
        },
        function () {
          return legacyCopy();
        }
      );
    }
    return Promise.resolve(legacyCopy());
  }

  function legacyCopy() {
    try {
      return document.execCommand("copy");
    } catch (e) {
      return false;
    }
  }

  document.querySelectorAll(".copybutton").forEach(function (button) {
    button.addEventListener("click", function () {
      var field =
        button.dataset.copyTarget === "prev"
          ? button.previousElementSibling
          : document.getElementById(button.dataset.copyTarget);
      if (!field) return;

      var original = button.textContent;
      copyText(field).then(function (done) {
        button.textContent = done ? "Скопировано" : "Выделено — Ctrl+C";
        setTimeout(function () {
          button.textContent = original;
        }, 2000);
      });
    });
  });

  // Долгие действия (диагностика идёт до минуты) блокируют кнопку и говорят,
  // что происходит: без этого оператор жмёт «Проверить» второй и третий раз.
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit], button:not([type])");
      if (!button) return;
      // disabled ставим следующим тиком: снятая прямо в обработчике submit
      // кнопка в части браузеров отменяет саму отправку формы.
      setTimeout(function () {
        button.disabled = true;
        button.dataset.busyActive = "1";
        button.textContent = form.dataset.busy;
      }, 0);
    });
  });
})();
