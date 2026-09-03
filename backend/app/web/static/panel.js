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
})();
