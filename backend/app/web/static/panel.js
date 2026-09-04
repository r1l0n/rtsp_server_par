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

      // Сохраняем innerHTML, а не textContent: внутри кнопки лежит ещё и
      // <svg> с иконкой, и подмена текстом стёрла бы её насовсем.
      var original = button.innerHTML;
      copyText(field).then(function (done) {
        button.textContent = done ? "Скопировано" : "Выделено — Ctrl+C";
        setTimeout(function () {
          button.innerHTML = original;
        }, 2000);
      });
    });
  });

  // Долгие действия (проверка камеры идёт до полуминуты) блокируют кнопку и
  // говорят, что происходит: без этого оператор жмёт «Проверить» второй и
  // третий раз. Признак висит на кнопке, а не на форме: в форме добавления
  // камеры две кнопки submit с разными formaction.
  document.querySelectorAll("button[data-busy]").forEach(function (button) {
    button.addEventListener("click", function () {
      // disabled ставим следующим тиком: снятая прямо в обработчике кнопка
      // в части браузеров отменяет саму отправку формы.
      setTimeout(function () {
        button.disabled = true;
        button.dataset.busyActive = "1";
        button.textContent = button.dataset.busy;
      }, 0);
    });
  });

  // Снапшота может не быть — камеру только что добавили или она не отвечает.
  // Битая картинка выглядит как поломка страницы, поэтому прячем её и
  // показываем подложку «кадра ещё нет».
  document.querySelectorAll("img[data-fallback-hide]").forEach(function (img) {
    img.addEventListener("error", function () {
      img.hidden = true;
    });
    // Скрипт отложенный, и к моменту его запуска событие error могло уже
    // пройти: у загруженной, но пустой картинки naturalWidth равен нулю.
    if (img.complete && img.naturalWidth === 0) img.hidden = true;
  });

  // ── Модальные окна ────────────────────────────────────────────────────────
  //
  // Просмотр камеры и выдача ссылки открываются поверх страницы, а не на
  // отдельном адресе: оператор не теряет место в списке и не ждёт перезагрузку.
  // Поток поднимается при открытии окна и глушится при закрытии — иначе камера
  // осталась бы подключённой после того, как окно закрыли.
  var openDialog = null;

  function playersIn(dialog) {
    if (!window.RTSPPlayer) return [];
    return Array.prototype.map
      .call(dialog.querySelectorAll(".player"), function (root) {
        return window.RTSPPlayer.get(root);
      })
      .filter(Boolean);
  }

  function open(dialog) {
    if (!dialog) return;
    close();
    dialog.hidden = false;
    openDialog = dialog;
    playersIn(dialog).forEach(function (player) {
      player.start();
    });
    var focusable = dialog.querySelector("input, select, textarea, button");
    if (focusable) focusable.focus();
  }

  function close() {
    if (!openDialog) return;
    playersIn(openDialog).forEach(function (player) {
      player.stop();
    });
    openDialog.hidden = true;
    openDialog = null;
  }

  document.querySelectorAll("[data-dialog-open], [data-watch]").forEach(function (button) {
    button.addEventListener("click", function () {
      var id = button.dataset.dialogOpen || button.dataset.watch;
      open(document.getElementById(id));
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", close);
  });

  // Клик по затемнению и Escape закрывают окно: и то и другое ожидаемо, а
  // ловушка «окно закрывается только крестиком» раздражает больше всего.
  document.querySelectorAll("[data-dialog]").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) close();
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") close();
  });

  // ── Фильтр списка ─────────────────────────────────────────────────────────
  // Список камер редко бывает длинным, поэтому фильтр честно клиентский:
  // серверный поиск потребовал бы перезагрузки страницы на каждую букву.
  document.querySelectorAll("[data-filter-target]").forEach(function (input) {
    var items = document.querySelectorAll(input.dataset.filterTarget);
    var empty = document.querySelector(".filter-empty");

    input.addEventListener("input", function () {
      var needle = input.value.trim().toLowerCase();
      var shown = 0;
      items.forEach(function (item) {
        var hay = (item.dataset.search || item.textContent || "").toLowerCase();
        var match = !needle || hay.indexOf(needle) !== -1;
        item.hidden = !match;
        if (match) shown += 1;
      });
      if (empty) empty.hidden = shown !== 0;
    });
  });
})();
