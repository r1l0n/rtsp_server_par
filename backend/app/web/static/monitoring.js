/* Страница мониторинга: опрос сервера и рисование графиков.
 *
 * Библиотеки графиков здесь нет намеренно. CSP разрешает скрипты только
 * 'self', то есть любую стороннюю библиотеку пришлось бы класть в репозиторий
 * и обновлять вручную — как это уже сделано с hls.js, но там иначе никак:
 * свой проигрыватель HLS не пишут. Графики же нужны ровно двух видов —
 * заливка с накоплением и линия, — и это триста строк холста без единой
 * зависимости.
 *
 * Цвета скрипт не знает: он читает их из таблицы стилей (--chart-1 и
 * соседние), поэтому смена темы меняет и графики.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-monitoring]");
  if (!root) return;

  var INTERVAL = Math.max(parseInt(root.dataset.interval, 10) || 5, 1);
  var RANGES = { "5m": 300, "15m": 900, "1h": 3600 };
  var range = RANGES[root.dataset.range] ? root.dataset.range : "15m";

  // ── Числа по-русски ───────────────────────────────────────────────────────
  function num(value, digits) {
    return Number(value).toLocaleString("ru-RU", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  }

  var BYTE_UNITS = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"];

  function scaleBytes(value) {
    var index = 0;
    var rest = Math.abs(value);
    while (rest >= 1024 && index < BYTE_UNITS.length - 1) {
      rest /= 1024;
      index += 1;
    }
    return { value: rest, unit: BYTE_UNITS[index], index: index };
  }

  function formatBytes(value) {
    var scaled = scaleBytes(value);
    // Знак после запятой — только у маленьких значений: «1,4 ГБ» полезно,
    // «847,3 МБ» — уже нет, лишняя цифра только мешает сравнивать.
    var digits = scaled.index > 0 && scaled.value < 100 ? 1 : 0;
    return num(scaled.value, digits) + " " + scaled.unit;
  }

  function formatRate(value) {
    return formatBytes(value) + "/с";
  }

  function formatPercent(value) {
    return num(value, 1) + " %";
  }

  // Подписи шкалы — отдельные форматы. На делениях нужны круглые числа:
  // «100 %» и «2 ГБ» читаются с одного взгляда, а «100,0 %» и «2,0 ГБ»
  // заставляют вглядываться в цифру, которая всегда ноль.
  function axisPercent(value) {
    return num(value, 0) + " %";
  }

  function axisBytes(value) {
    var scaled = scaleBytes(value);
    var digits = Number.isInteger(scaled.value) || scaled.value >= 100 ? 0 : 1;
    return num(scaled.value, digits) + " " + scaled.unit;
  }

  function axisRate(value) {
    return axisBytes(value) + "/с";
  }

  function plural(count, one, few, many) {
    var tail100 = count % 100;
    var tail10 = count % 10;
    if (tail100 >= 11 && tail100 <= 14) return many;
    if (tail10 === 1) return one;
    if (tail10 >= 2 && tail10 <= 4) return few;
    return many;
  }

  function formatUptime(seconds) {
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return days + " " + plural(days, "день", "дня", "дней") + " " + hours + " ч";
    if (hours > 0) return hours + " ч " + minutes + " мин";
    return minutes + " " + plural(minutes, "минута", "минуты", "минут");
  }

  function formatClock(unixSeconds, withSeconds) {
    var options = withSeconds
      ? { hour: "2-digit", minute: "2-digit", second: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" };
    return new Date(unixSeconds * 1000).toLocaleTimeString("ru-RU", options);
  }

  // ── Палитра из таблицы стилей ─────────────────────────────────────────────
  // Значения переменных CSS приезжают как написаны в файле («#3987e5»),
  // поэтому прозрачность приходится собирать самим.
  var paletteCache = null;

  function palette() {
    if (paletteCache) return paletteCache;
    var styles = getComputedStyle(document.documentElement);
    function token(name) {
      return styles.getPropertyValue(name).trim();
    }
    paletteCache = {
      series: [token("--chart-1"), token("--chart-2"), token("--chart-3"),
               token("--chart-4"), token("--chart-5")],
      grid: token("--chart-grid"),
      surface: token("--surface"),
      text: token("--text"),
      muted: token("--neutral-400"),
      ok: token("--ok"),
      warn: token("--warn"),
      danger: token("--danger"),
      // Шрифт подписей на холсте. Читается здесь, а не при каждой отрисовке:
      // getComputedStyle заставляет браузер пересчитать стили, а рисуем мы
      // десять графиков каждые несколько секунд.
      font: "11px " + getComputedStyle(document.body).fontFamily
    };
    return paletteCache;
  }

  if (window.matchMedia) {
    // Тема «как в системе» может переключиться, пока страница открыта.
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      paletteCache = null;
      redrawAll();
    });
  }

  function withAlpha(color, alpha) {
    var hex = color.replace("#", "");
    if (hex.length === 3) {
      hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    }
    if (hex.length !== 6 || /[^0-9a-f]/i.test(hex)) return color;
    var value = parseInt(hex, 16);
    return "rgba(" + ((value >> 16) & 255) + "," + ((value >> 8) & 255) + ","
      + (value & 255) + "," + alpha + ")";
  }

  // ── График ────────────────────────────────────────────────────────────────
  //
  // Один объект на холст. Данные приходят массивом точек вида
  // {t: время, v: [значения рядов]}; ряды описываются при создании.
  var charts = [];

  function createChart(host, spec) {
    var canvas = document.createElement("canvas");
    canvas.setAttribute("aria-hidden", "true");
    host.appendChild(canvas);

    var tip = null;
    if (!spec.spark) {
      tip = document.createElement("div");
      tip.className = "mon-tip";
      tip.hidden = true;
      host.appendChild(tip);
      // Значения доступны и без мыши: с клавиатуры поле графика получает
      // фокус, а стрелки двигают перекрестье.
      host.tabIndex = 0;
      host.setAttribute("role", "group");
      host.setAttribute("aria-label", spec.title || "График");
    }

    var chart = {
      host: host,
      canvas: canvas,
      ctx: canvas.getContext("2d"),
      tip: tip,
      spec: spec,
      points: [],
      domain: [0, 0],
      hover: -1
    };

    if (tip) bindPointer(chart);
    charts.push(chart);
    return chart;
  }

  function seriesColor(chart, index) {
    var slot = chart.spec.series[index].slot;
    return palette().series[slot % palette().series.length];
  }

  function geometry(chart, width, height) {
    var left = chart.spec.spark ? 0 : 56;
    var bottom = chart.spec.spark ? 0 : 22;
    var top = chart.spec.spark ? 4 : 10;
    var right = chart.spec.spark ? 0 : 12;
    return {
      left: left,
      top: top,
      width: Math.max(width - left - right, 1),
      height: Math.max(height - top - bottom, 1)
    };
  }

  function niceCeiling(value, binary) {
    if (!(value > 0)) return binary ? 1024 : 1;
    if (binary) {
      var scaled = scaleBytes(value);
      var step = Math.pow(1024, scaled.index);
      return niceCeiling(scaled.value, false) * step;
    }
    var power = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var rest = value / power;
    var nice = rest <= 1 ? 1 : rest <= 2 ? 2 : rest <= 2.5 ? 2.5 : rest <= 5 ? 5 : 10;
    return nice * power;
  }

  function upperBound(chart) {
    if (typeof chart.spec.max === "number") return chart.spec.max;
    var peak = 0;
    for (var i = 0; i < chart.points.length; i += 1) {
      var values = chart.points[i].v;
      if (chart.spec.stacked) {
        var sum = 0;
        for (var s = 0; s < values.length; s += 1) sum += values[s] || 0;
        peak = Math.max(peak, sum);
      } else {
        for (var k = 0; k < values.length; k += 1) peak = Math.max(peak, values[k] || 0);
      }
    }
    // Пол шкалы: без него простаивающий сервер рисует шум в полный рост,
    // и «12 байт в секунду» выглядит как всплеск трафика.
    return Math.max(niceCeiling(peak, chart.spec.binary), chart.spec.floor || 1);
  }

  function draw(chart) {
    var canvas = chart.canvas;
    var width = canvas.clientWidth;
    var height = canvas.clientHeight;
    if (!width || !height) return;

    var ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
    if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);

    var ctx = chart.ctx;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    var colors = palette();
    var box = geometry(chart, width, height);
    var top = upperBound(chart);
    var from = chart.domain[0];
    var to = chart.domain[1];
    var span = Math.max(to - from, 1);

    function x(time) {
      return box.left + ((time - from) / span) * box.width;
    }
    function y(value) {
      return box.top + box.height - (Math.min(value, top) / top) * box.height;
    }

    if (!chart.spec.spark) drawGrid(ctx, chart, box, colors, top, x, from, span);

    if (chart.points.length) {
      if (chart.spec.stacked) drawStack(ctx, chart, x, y);
      else drawLines(ctx, chart, x, y, colors);
    }

    if (!chart.spec.spark && chart.hover >= 0 && chart.hover < chart.points.length) {
      drawCrosshair(ctx, chart, box, colors, x, y);
    }
  }

  function drawGrid(ctx, chart, box, colors, top, x, from, span) {
    var lines = 4;
    ctx.save();
    ctx.strokeStyle = colors.grid;
    ctx.fillStyle = colors.muted;
    ctx.lineWidth = 1;
    ctx.font = colors.font;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    for (var i = 0; i <= lines; i += 1) {
      var value = (top / lines) * i;
      // Полупиксельный сдвиг: линия толщиной в пиксель, положенная на целую
      // координату, размазывается на два ряда точек и выглядит серой.
      var py = Math.round(box.top + box.height - (box.height / lines) * i) + 0.5;
      ctx.beginPath();
      ctx.moveTo(box.left, py);
      ctx.lineTo(box.left + box.width, py);
      ctx.stroke();
      ctx.fillText((chart.spec.axis || chart.spec.format)(value), box.left - 8, py);
    }

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var ticks = box.width > 420 ? 5 : 3;
    var seconds = span <= 600;
    for (var t = 0; t <= ticks; t += 1) {
      var moment = from + (span / ticks) * t;
      var px = x(moment);
      if (t === 0) ctx.textAlign = "left";
      else if (t === ticks) ctx.textAlign = "right";
      else ctx.textAlign = "center";
      ctx.fillText(formatClock(moment, seconds), px, box.top + box.height + 6);
    }
    ctx.restore();
  }

  //: Разрыв в данных. Сборщик мог быть остановлен — соединять точки через
  //: такую дыру нельзя: получится прямая, которой не было ни секунды.
  function isGap(previous, current) {
    return current.t - previous.t > INTERVAL * 3;
  }

  function drawStack(ctx, chart, x, y) {
    var count = chart.spec.series.length;
    var totals = new Array(chart.points.length);
    for (var p = 0; p < chart.points.length; p += 1) totals[p] = 0;

    for (var s = 0; s < count; s += 1) {
      var color = seriesColor(chart, s);
      var segments = [];
      var current = null;
      for (var i = 0; i < chart.points.length; i += 1) {
        if (i > 0 && isGap(chart.points[i - 1], chart.points[i])) current = null;
        if (!current) {
          current = [];
          segments.push(current);
        }
        var value = chart.points[i].v[s] || 0;
        current.push({ t: chart.points[i].t, low: totals[i], high: totals[i] + value });
        totals[i] += value;
      }

      for (var g = 0; g < segments.length; g += 1) {
        var band = segments[g];
        if (!band.length) continue;
        ctx.beginPath();
        ctx.moveTo(x(band[0].t), y(band[0].low));
        for (var a = 0; a < band.length; a += 1) ctx.lineTo(x(band[a].t), y(band[a].high));
        for (var b = band.length - 1; b >= 0; b -= 1) ctx.lineTo(x(band[b].t), y(band[b].low));
        ctx.closePath();
        // Заливка — намеренно бледная: полоса показывает долю, а читают
        // график по верхней кромке.
        ctx.fillStyle = withAlpha(color, 0.22);
        ctx.fill();

        ctx.beginPath();
        for (var c = 0; c < band.length; c += 1) {
          var px = x(band[c].t);
          var py = y(band[c].high);
          if (c === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.stroke();
      }
    }
  }

  function drawLines(ctx, chart, x, y, colors) {
    // Путь линии прокладывается дважды: один раз для заливки, второй — для
    // обводки. Обвести тот же путь, что залит, нельзя — он замкнут по низу
    // графика, и вдоль оси легла бы лишняя линия.
    function trace(index) {
      ctx.beginPath();
      var started = false;
      for (var i = 0; i < chart.points.length; i += 1) {
        var point = chart.points[i];
        if (i > 0 && isGap(chart.points[i - 1], point)) started = false;
        var px = x(point.t);
        var py = y(point.v[index] || 0);
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
        }
      }
    }

    for (var s = 0; s < chart.spec.series.length; s += 1) {
      var color = seriesColor(chart, s);
      if (chart.spec.spark || chart.spec.series.length === 1) {
        // Один ряд — заливаем подложку: она задаёт объём и делает
        // маленький график читаемым без подписей.
        var last = chart.points[chart.points.length - 1];
        var first = chart.points[0];
        trace(s);
        ctx.lineTo(x(last.t), y(0));
        ctx.lineTo(x(first.t), y(0));
        ctx.closePath();
        ctx.fillStyle = withAlpha(color, 0.14);
        ctx.fill();
      }
      trace(s);
      ctx.strokeStyle = color;
      ctx.lineWidth = chart.spec.spark ? 1.5 : 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();

      if (!chart.spec.spark && chart.points.length) {
        // Точка на конце линии: она отвечает на вопрос «а сейчас сколько»,
        // и её же видно, когда данных всего один замер и линии ещё нет.
        var end = chart.points[chart.points.length - 1];
        ctx.beginPath();
        ctx.arc(x(end.t), y(end.v[s] || 0), 3.5, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = colors.surface;
        ctx.stroke();
      }
    }
  }

  function drawCrosshair(ctx, chart, box, colors, x, y) {
    var point = chart.points[chart.hover];
    var px = Math.round(x(point.t)) + 0.5;
    ctx.save();
    ctx.strokeStyle = colors.muted;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, box.top);
    ctx.lineTo(px, box.top + box.height);
    ctx.stroke();

    var stack = 0;
    for (var s = 0; s < chart.spec.series.length; s += 1) {
      var value = point.v[s] || 0;
      stack += value;
      var py = y(chart.spec.stacked ? stack : value);
      ctx.beginPath();
      ctx.arc(px, py, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = seriesColor(chart, s);
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = colors.surface;
      ctx.stroke();
    }
    ctx.restore();
  }

  // ── Перекрестье и подсказка ───────────────────────────────────────────────
  function nearestIndex(chart, offsetX) {
    if (!chart.points.length) return -1;
    var width = chart.canvas.clientWidth;
    var box = geometry(chart, width, chart.canvas.clientHeight);
    var from = chart.domain[0];
    var span = Math.max(chart.domain[1] - from, 1);
    var time = from + ((offsetX - box.left) / box.width) * span;
    var best = 0;
    var bestDistance = Infinity;
    for (var i = 0; i < chart.points.length; i += 1) {
      var distance = Math.abs(chart.points[i].t - time);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = i;
      }
    }
    return best;
  }

  function showTip(chart) {
    var point = chart.points[chart.hover];
    if (!point) return;
    var tip = chart.tip;
    tip.textContent = "";

    var head = document.createElement("div");
    head.className = "mon-tip-time";
    head.textContent = formatClock(point.t, true);
    tip.appendChild(head);

    for (var s = 0; s < chart.spec.series.length; s += 1) {
      var row = document.createElement("div");
      row.className = "mon-tip-row";

      var mark = document.createElement("span");
      mark.className = "mon-tip-mark";
      mark.style.setProperty("--mon-key", seriesColor(chart, s));
      row.appendChild(mark);

      var name = document.createElement("span");
      name.className = "mon-tip-name";
      // textContent, а не разметка строкой: имена интерфейсов и дисков
      // приходят с сервера, и склеивать из них HTML нельзя.
      name.textContent = chart.spec.series[s].name;
      row.appendChild(name);

      var value = document.createElement("span");
      value.className = "mon-tip-value";
      value.textContent = chart.spec.format(point.v[s] || 0);
      row.appendChild(value);

      tip.appendChild(row);
    }

    tip.hidden = false;
    var box = geometry(chart, chart.canvas.clientWidth, chart.canvas.clientHeight);
    var from = chart.domain[0];
    var span = Math.max(chart.domain[1] - from, 1);
    var px = box.left + ((point.t - from) / span) * box.width;
    var width = tip.offsetWidth;
    // Подсказка держится сбоку от перекрестья и не вылезает за карточку:
    // у правого края она переезжает налево.
    var left = px + 14;
    if (left + width > chart.canvas.clientWidth) left = Math.max(px - width - 14, 0);
    tip.style.setProperty("--tip-x", Math.round(left) + "px");
    tip.style.setProperty("--tip-y", "10px");
  }

  function hideTip(chart) {
    chart.hover = -1;
    if (chart.tip) chart.tip.hidden = true;
    draw(chart);
  }

  function bindPointer(chart) {
    chart.host.addEventListener("pointermove", function (event) {
      var bounds = chart.canvas.getBoundingClientRect();
      var index = nearestIndex(chart, event.clientX - bounds.left);
      if (index < 0) return;
      chart.hover = index;
      draw(chart);
      showTip(chart);
    });
    chart.host.addEventListener("pointerleave", function () {
      hideTip(chart);
    });
    chart.host.addEventListener("blur", function () {
      hideTip(chart);
    });
    chart.host.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (!chart.points.length) return;
      event.preventDefault();
      var start = chart.hover < 0 ? chart.points.length - 1 : chart.hover;
      var next = start + (event.key === "ArrowRight" ? 1 : -1);
      chart.hover = Math.min(Math.max(next, 0), chart.points.length - 1);
      draw(chart);
      showTip(chart);
    });
  }

  function redrawAll() {
    for (var i = 0; i < charts.length; i += 1) draw(charts[i]);
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(redrawAll, 120);
  });

  // ── Сборка страницы ───────────────────────────────────────────────────────
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function slot(name) {
    return root.querySelector('[data-mon="' + name + '"]');
  }

  function section(name) {
    return root.querySelector('[data-mon-section="' + name + '"]');
  }

  // Легенда обязательна, когда рядов больше одного, и она же показывает
  // текущее значение каждого ряда: цвет один опознавать не должен, а число
  // не должно прятаться под курсором.
  function fillLegend(host, series, values) {
    host.textContent = "";
    for (var i = 0; i < series.length; i += 1) {
      var key = el("span", "mon-key");
      var mark = el("span", "mon-key-mark");
      mark.style.setProperty("--mon-key", palette().series[series[i].slot]);
      key.appendChild(mark);
      key.appendChild(el("span", "mon-key-name", series[i].name));
      key.appendChild(el("span", "mon-key-value", values[i]));
      host.appendChild(key);
    }
  }

  function legendFor(name, series, values) {
    var host = root.querySelector('[data-mon-legend="' + name + '"]');
    if (host) fillLegend(host, series, values);
  }

  var CPU_SERIES = [
    { name: "программы", slot: 0 },
    { name: "ядро", slot: 1 },
    { name: "ожидание диска", slot: 2 },
    { name: "отнято гипервизором", slot: 3 }
  ];

  var MEMORY_SERIES = [
    { name: "занято", slot: 0 },
    { name: "кэш", slot: 1 },
    { name: "буферы", slot: 2 }
  ];

  var TRAFFIC_SERIES = [
    { name: "приём", slot: 0 },
    { name: "передача", slot: 1 }
  ];

  var DISK_SERIES = [
    { name: "чтение", slot: 0 },
    { name: "запись", slot: 1 }
  ];

  var cpuChart = createChart(root.querySelector('[data-mon-chart="cpu"]'), {
    title: "Загрузка процессора",
    series: CPU_SERIES,
    stacked: true,
    max: 100,
    format: formatPercent,
    axis: axisPercent
  });

  var memoryChart = createChart(root.querySelector('[data-mon-chart="memory"]'), {
    title: "Использование памяти",
    series: MEMORY_SERIES,
    stacked: true,
    binary: true,
    format: formatBytes,
    axis: axisBytes
  });

  var sparks = {};
  Array.prototype.forEach.call(root.querySelectorAll("[data-mon-spark]"), function (canvas) {
    var name = canvas.dataset.monSpark;
    // Спарклайн рисуется на своём холсте, который уже лежит в разметке, —
    // createChart создаёт холст сам, поэтому берём его собственный объект.
    var chart = {
      host: canvas.parentNode,
      canvas: canvas,
      ctx: canvas.getContext("2d"),
      tip: null,
      spec: { spark: true, series: [{ name: name, slot: 0 }], format: formatPercent },
      points: [],
      domain: [0, 0],
      hover: -1
    };
    charts.push(chart);
    sparks[name] = chart;
  });

  // Графики интерфейсов и дисков появляются по факту: сколько железа нашлось,
  // столько и карточек. Пересоздаём их только когда меняется сам список —
  // иначе каждое обновление стирало бы наведённую подсказку.
  var dynamic = { net: {}, disk: {} };

  function ensureCells(kind, names, host, series, spec) {
    var known = Object.keys(dynamic[kind]);
    var same = known.length === names.length && known.every(function (name) {
      return names.indexOf(name) !== -1;
    });
    if (same) return;

    for (var i = 0; i < known.length; i += 1) {
      var at = charts.indexOf(dynamic[kind][known[i]].chart);
      if (at >= 0) charts.splice(at, 1);
    }
    dynamic[kind] = {};
    host.textContent = "";

    names.forEach(function (name) {
      var cell = el("div", "mon-cell");
      var head = el("div", "mon-head");
      var title = el("div");
      title.appendChild(el("div", "mon-cell-title", name));
      head.appendChild(title);
      var legend = el("div", "mon-legend");
      head.appendChild(legend);
      cell.appendChild(head);

      var plot = el("div", "mon-plot mon-plot-sm");
      cell.appendChild(plot);
      host.appendChild(cell);

      var chart = createChart(plot, {
        title: name,
        series: series,
        binary: true,
        floor: spec.floor,
        format: formatRate,
        axis: axisRate
      });
      dynamic[kind][name] = { chart: chart, legend: legend };
    });
  }

  function meter(name, valueText, ratio, tone) {
    var row = el("div", "mon-meter");
    row.appendChild(el("div", "mon-meter-name", name));
    var bar = el("div", "mon-bar");
    var fill = el("span", "mon-bar-fill");
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("div", "mon-meter-value", valueText));
    bar.style.setProperty("--mon-fill", tone);
    fill.style.setProperty("--mon-value", Math.min(Math.max(ratio, 0), 1) * 100 + "%");
    return row;
  }

  // ── Состояние и опрос ─────────────────────────────────────────────────────
  var samples = [];
  var lastAt = 0;
  var failures = 0;
  var timer = null;

  function windowSeconds() {
    return RANGES[range];
  }

  function trim(now) {
    var cutoff = now - windowSeconds();
    while (samples.length && samples[0].at < cutoff) samples.shift();
  }

  function points(pick) {
    var result = [];
    for (var i = 0; i < samples.length; i += 1) {
      result.push({ t: samples[i].at, v: pick(samples[i]) });
    }
    return result;
  }

  function apply(chart, pickList, domain) {
    chart.points = points(pickList);
    chart.domain = domain;
    if (chart.hover >= chart.points.length) chart.hover = chart.points.length - 1;
    draw(chart);
  }

  function latest() {
    return samples.length ? samples[samples.length - 1] : null;
  }

  function render(payload) {
    var now = payload.server_time;
    trim(now);
    var domain = [now - windowSeconds(), now];
    var last = latest();

    renderTiles(last, payload);
    renderCpu(domain, last);
    renderMemory(domain, last);
    renderNetwork(domain, last);
    renderDisks(domain, last);
    renderState(payload.state);
    renderService(payload.service);

    var updated = slot("updated");
    if (updated) {
      updated.textContent = last
        ? "Последнее измерение: " + formatClock(last.at, true)
        : "";
    }
  }

  function renderTiles(last, payload) {
    var state = payload.state || {};
    if (last) {
      setTile("cpu", formatPercent(last.cpu.busy),
        (state.cores || 1) + " " + plural(state.cores || 1, "ядро", "ядра", "ядер"));
      var memory = last.memory;
      var share = memory.total ? (memory.used / memory.total) * 100 : 0;
      setTile("memory", formatPercent(share),
        formatBytes(memory.used) + " из " + formatBytes(memory.total));
      var traffic = totals(last.net);
      setTile("net", formatRate(traffic[0] + traffic[1]),
        "приём " + formatRate(traffic[0]) + " · передача " + formatRate(traffic[1]));
      var io = totals(last.disk);
      setTile("disk", formatRate(io[0] + io[1]),
        "чтение " + formatRate(io[0]) + " · запись " + formatRate(io[1]));
      setTile("load", num(last.load[0], 2),
        "за 5 мин " + num(last.load[1], 2) + " · за 15 мин " + num(last.load[2], 2));
    }
    if (state.uptime) {
      setTile("uptime", formatUptime(state.uptime), "без перезагрузки");
    }

    if (last) {
      sparkline("cpu", function (sample) { return [sample.cpu.busy]; }, 100, false);
      sparkline("memory", function (sample) {
        return [sample.memory.total ? (sample.memory.used / sample.memory.total) * 100 : 0];
      }, 100, false);
      sparkline("net", function (sample) {
        var pair = totals(sample.net);
        return [pair[0] + pair[1]];
      }, null, true);
      sparkline("disk", function (sample) {
        var pair = totals(sample.disk);
        return [pair[0] + pair[1]];
      }, null, true);
    }
  }

  // Итог по интерфейсам и дискам. Мосты docker'а в сумму не идут: трафик
  // контейнера проходит и через мост, и через сетевую карту, поэтому в общем
  // числе он оказался бы дважды.
  function totals(rows) {
    var first = 0;
    var second = 0;
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].bridge) continue;
      first += rows[i].rx !== undefined ? rows[i].rx : rows[i].read;
      second += rows[i].tx !== undefined ? rows[i].tx : rows[i].write;
    }
    return [first, second];
  }

  function sparkline(name, pick, max, binary) {
    var chart = sparks[name];
    if (!chart) return;
    chart.spec.max = max === null ? undefined : max;
    chart.spec.binary = binary;
    chart.spec.floor = binary ? 65536 : 1;
    chart.points = points(pick);
    chart.domain = [samples[0] ? samples[0].at : 0, latest().at];
    draw(chart);
  }

  function setTile(name, value, note) {
    var valueNode = slot(name + ".value");
    var noteNode = slot(name + ".note");
    if (valueNode) valueNode.textContent = value;
    if (noteNode) noteNode.textContent = note || "";
  }

  function renderCpu(domain, last) {
    apply(cpuChart, function (sample) {
      return [sample.cpu.user, sample.cpu.system, sample.cpu.iowait, sample.cpu.steal];
    }, domain);

    if (last) {
      legendFor("cpu", CPU_SERIES, [
        formatPercent(last.cpu.user), formatPercent(last.cpu.system),
        formatPercent(last.cpu.iowait), formatPercent(last.cpu.steal)
      ]);
      renderCores(last.cpu.cores || []);
    }
  }

  function renderCores(cores) {
    var host = slot("cores");
    if (!host) return;
    host.textContent = "";
    for (var i = 0; i < cores.length; i += 1) {
      var cell = el("div", "mon-core");
      cell.appendChild(el("div", "mon-core-name", "Ядро " + i));
      cell.appendChild(el("div", "mon-core-value", formatPercent(cores[i])));
      var bar = el("div", "mon-bar");
      var fill = el("span", "mon-bar-fill");
      bar.appendChild(fill);
      bar.style.setProperty("--mon-fill", palette().series[0]);
      fill.style.setProperty("--mon-value", Math.min(cores[i], 100) + "%");
      cell.appendChild(bar);
      host.appendChild(cell);
    }
  }

  function renderMemory(domain, last) {
    memoryChart.spec.max = last && last.memory.total ? last.memory.total : undefined;
    apply(memoryChart, function (sample) {
      return [sample.memory.used, sample.memory.cached, sample.memory.buffers];
    }, domain);

    if (last) {
      legendFor("memory", MEMORY_SERIES, [
        formatBytes(last.memory.used),
        formatBytes(last.memory.cached),
        formatBytes(last.memory.buffers)
      ]);
    }
  }

  // Общий потолок шкалы на все карточки одного вида. Иначе тихий интерфейс
  // рисуется той же высоты, что и загруженный, — только с другими подписями
  // сбоку, — и рядом друг с другом они врут о соотношении.
  function sharedCeiling(rows, first, second, floor) {
    var peak = 0;
    for (var i = 0; i < samples.length; i += 1) {
      var list = samples[i][rows] || [];
      for (var k = 0; k < list.length; k += 1) {
        peak = Math.max(peak, list[k][first] || 0, list[k][second] || 0);
      }
    }
    return Math.max(niceCeiling(peak, true), floor);
  }

  function renderNetwork(domain, last) {
    var host = slot("net.charts");
    var card = section("net");
    if (!host || !card) return;
    var names = last ? last.net.map(function (row) { return row.name; }) : [];
    card.hidden = names.length === 0;
    if (!names.length) return;

    ensureCells("net", names, host, TRAFFIC_SERIES, { floor: 65536 });
    var ceiling = sharedCeiling("net", "rx", "tx", 65536);
    names.forEach(function (name, index) {
      var cell = dynamic.net[name];
      if (!cell) return;
      cell.chart.spec.max = ceiling;
      apply(cell.chart, function (sample) {
        var row = sample.net[index];
        return row && row.name === name ? [row.rx, row.tx] : [0, 0];
      }, domain);
      var row = last.net[index];
      fillLegend(cell.legend, TRAFFIC_SERIES, [formatRate(row.rx), formatRate(row.tx)]);
    });
  }

  function renderDisks(domain, last) {
    var host = slot("disk.charts");
    var card = section("disk");
    if (!host || !card) return;
    var names = last ? last.disk.map(function (row) { return row.name; }) : [];
    card.hidden = names.length === 0;
    if (!names.length) return;

    ensureCells("disk", names, host, DISK_SERIES, { floor: 65536 });
    var ceiling = sharedCeiling("disk", "read", "write", 65536);
    names.forEach(function (name, index) {
      var cell = dynamic.disk[name];
      if (!cell) return;
      cell.chart.spec.max = ceiling;
      apply(cell.chart, function (sample) {
        var row = sample.disk[index];
        return row && row.name === name ? [row.read, row.write] : [0, 0];
      }, domain);
      var row = last.disk[index];
      fillLegend(cell.legend, DISK_SERIES, [
        formatRate(row.read), formatRate(row.write)
      ]);
      cell.legend.appendChild(el("span", "mon-key",
        "занят " + formatPercent(row.busy)));
    });
  }

  function renderState(state) {
    if (!state) return;
    renderFilesystems(state.filesystems || []);
    renderTemperatures(state.temperatures || []);
    renderProcesses(state.processes);

    var hint = slot("net.hint");
    if (hint) {
      hint.textContent = state.host_network
        ? "Приём и передача по каждому интерфейсу сервера"
        : "Видны интерфейсы контейнера: procfs хоста не примонтирован";
    }
  }

  function renderFilesystems(rows) {
    var host = slot("fs");
    var card = section("fs");
    if (!host || !card) return;
    card.hidden = rows.length === 0;
    host.textContent = "";
    for (var i = 0; i < rows.length; i += 1) {
      var row = rows[i];
      var share = row.total ? row.used / row.total : 0;
      // Заполненный раздел — это состояние, а не ряд данных, поэтому здесь
      // цвет значит «хорошо/тревога/беда», а не «имя».
      var tone = share > 0.9 ? palette().danger
        : share > 0.75 ? palette().warn : palette().ok;
      host.appendChild(meter(
        row.mount,
        formatBytes(row.used) + " из " + formatBytes(row.total)
          + " · свободно " + formatBytes(row.free),
        share,
        tone
      ));
    }
  }

  function renderTemperatures(rows) {
    var host = slot("temp");
    var card = section("temp");
    if (!host || !card) return;
    card.hidden = rows.length === 0;
    host.textContent = "";
    for (var i = 0; i < rows.length; i += 1) {
      var value = rows[i].celsius;
      var tone = value > 80 ? palette().danger : value > 65 ? palette().warn : palette().ok;
      host.appendChild(meter(rows[i].label, num(value, 1) + " °C", value / 100, tone));
    }
  }

  function renderProcesses(processes) {
    var body = slot("procs");
    var card = section("procs");
    if (!body || !card || !processes) return;
    var rows = processes.top || [];
    card.hidden = rows.length === 0;
    body.textContent = "";
    for (var i = 0; i < rows.length; i += 1) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", null, rows[i].name));
      tr.appendChild(el("td", "mon-num", num(rows[i].cpu, 1)));
      tr.appendChild(el("td", "mon-num", formatBytes(rows[i].rss)));
      body.appendChild(tr);
    }
    var note = slot("procs.note");
    if (note) {
      note.textContent = "Всего " + processes.total + ", выполняется " + processes.running
        + ". Сто процентов — одно полностью занятое ядро";
    }
  }

  // Подписи склоняются по числу: «1 камера», «2 камеры», «5 камер». Без
  // этого в панели живёт «1 камер», и выглядит это как недоделка.
  var CAMERA = ["камера", "камеры", "камер"];
  var SERVICE_FACTS = [
    { key: "online", words: CAMERA, tail: " в эфире" },
    { key: "idle", words: ["ждёт", "ждут", "ждут"], tail: " зрителя" },
    { key: "offline", words: ["без связи", "без связи", "без связи"], tail: "" },
    { key: "error", words: ["с ошибкой", "с ошибкой", "с ошибкой"], tail: "" }
  ];

  function renderService(service) {
    var host = slot("service");
    if (!host || !service) return;
    host.textContent = "";

    function fact(value, label) {
      var cell = el("div", "mon-fact");
      cell.appendChild(el("div", "mon-fact-value", String(value)));
      cell.appendChild(el("div", "mon-fact-label", label));
      host.appendChild(cell);
    }

    function word(count, words) {
      return plural(count, words[0], words[1], words[2]);
    }

    fact(service.cameras_total, word(service.cameras_total, CAMERA) + " всего");
    for (var i = 0; i < SERVICE_FACTS.length; i += 1) {
      var count = service.cameras[SERVICE_FACTS[i].key] || 0;
      if (count) fact(count, word(count, SERVICE_FACTS[i].words) + SERVICE_FACTS[i].tail);
    }
    fact(service.links, word(service.links,
      ["действующая ссылка", "действующие ссылки", "действующих ссылок"]));
    fact(service.viewers,
      word(service.viewers, ["зритель", "зрителя", "зрителей"]) + " сейчас");
  }

  // ── Запросы ───────────────────────────────────────────────────────────────
  function load(reset) {
    var url = "/admin/monitoring/data?range=" + encodeURIComponent(range);
    if (!reset && lastAt) url += "&since=" + encodeURIComponent(lastAt);

    return fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then(function (payload) {
        failures = 0;
        root.classList.remove("mon-stale");
        if (reset) samples = [];
        for (var i = 0; i < payload.series.length; i += 1) {
          samples.push(payload.series[i]);
          lastAt = payload.series[i].at;
        }
        problem(payload.available ? "" : payload.reason);
        render(payload);
      })
      .catch(function () {
        failures += 1;
        root.classList.add("mon-stale");
        if (failures >= 3) {
          problem("Данные не обновляются: сервер не отвечает на запрос показаний.");
        }
      });
  }

  function problem(message) {
    var box = root.querySelector("[data-mon-problem]");
    if (!box) return;
    box.textContent = message;
    box.hidden = !message;
  }

  function schedule() {
    if (timer) window.clearInterval(timer);
    timer = window.setInterval(function () {
      // Вкладка в фоне не опрашивает сервер: страница мониторинга живёт
      // открытой часами, и невидимая она незачем нагружала бы базу.
      if (!document.hidden) load(false);
    }, INTERVAL * 1000);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) load(false);
  });

  Array.prototype.forEach.call(root.ownerDocument.querySelectorAll("[data-mon-range]"),
    function (button) {
      button.addEventListener("click", function () {
        range = RANGES[button.dataset.monRange] ? button.dataset.monRange : range;
        Array.prototype.forEach.call(
          root.ownerDocument.querySelectorAll("[data-mon-range]"),
          function (other) {
            var active = other === button;
            other.classList.toggle("is-active", active);
            other.setAttribute("aria-pressed", active ? "true" : "false");
          }
        );
        lastAt = 0;
        root.classList.add("mon-stale");
        load(true);
      });
    });

  load(true);
  schedule();
})();
