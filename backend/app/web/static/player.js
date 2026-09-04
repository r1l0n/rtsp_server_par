/*
 * Плеер.
 *
 * Транспортов у медиа-сервера для браузера ровно два, и они честно
 * дополняют друг друга:
 *
 *   WebRTC (WHEP) — задержка около секунды, но нужен UDP (или ICE поверх TCP)
 *                   и кодек, который умеет WebRTC: H.264/VP8/VP9/AV1.
 *   LL-HLS        — идёт по тому же 443/TCP, что и страница, поэтому проходит
 *                   везде; задержка 2–4 секунды. Умеет ещё и H.265, но такой
 *                   поток играет не всякий браузер.
 *
 * По умолчанию режим «Авто»: сначала WebRTC, при неудаче — HLS. Кнопками в
 * панели транспорт можно зафиксировать вручную: это единственный способ
 * быстро понять, что именно не работает в конкретной сети. Выбор запоминается
 * на вкладку.
 *
 * Отдельно ловим ситуацию «соединение установилось, а кадров нет»: именно так
 * выглядит несовместимый кодек, и без явного сообщения это неотличимо от
 * «ничего не работает».
 *
 * Экземпляров на странице может быть несколько (карточка камеры, модальное
 * окно списка), поэтому всё состояние живёт в замыкании одного .player, а
 * элементы ищутся внутри него — не по id, как было, пока плеер existовал
 * ровно в одном экземпляре на отдельной странице.
 *
 * Два способа получить адреса потока:
 *
 *   data-whep/data-hls — адреса уже известны (страница публичной ссылки:
 *                        доступ выдан cookie при её открытии);
 *   data-ticket        — панель. POST по этому адресу выдаёт оператору
 *                        временный доступ к своей камере и возвращает адреса.
 *                        Пока идёт просмотр, доступ продлевается: WebRTC после
 *                        установки соединения идёт мимо Caddy, и сам по себе
 *                        разрешение не продлевает.
 */
(function () {
  "use strict";

  var ICE_GATHER_TIMEOUT = 3000;
  var WEBRTC_CONNECT_TIMEOUT = 8000;
  //: Сколько ждём первый кадр после того, как транспорт отчитался об успехе.
  var FIRST_FRAME_TIMEOUT = 7000;
  var RETRY_BASE = 2000;
  var RETRY_MAX = 30000;
  //: Доступ оператора живёт 15 минут — продлеваем заметно раньше.
  var TICKET_REFRESH = 9 * 60 * 1000;

  var DENIED =
    "срок доступа по этой ссылке истёк. Откройте присланную вам ссылку " +
    "заново — она выдаст доступ ещё раз";

  var NO_FRAMES =
    "Поток подключился, но кадры не приходят — обычно это несовместимый " +
    "кодек камеры (H.265). Попробуйте другой транспорт или включите " +
    "перекодирование в панели.";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function create(root) {
    var video = root.querySelector("video");
    if (!video) return null;

    var statusEl = root.querySelector(".player-status");
    var transportEl = root.querySelector(".player-transport");
    var posterEl = root.querySelector(".player-poster");
    var soundBtn = root.querySelector(".player-sound");
    var stopBtn = root.querySelector(".player-stop");
    var fullscreenBtn = root.querySelector(".player-fullscreen");
    var modeButtons = root.querySelectorAll(".player-mode");

    var ticketUrl = root.dataset.ticket || "";
    var whepUrl = root.dataset.whep || "";
    var hlsUrl = root.dataset.hls || "";
    var wantAudio = root.dataset.audio === "1";
    var storageKey = "rtspgw.mode." + (root.dataset.key || whepUrl || ticketUrl);

    var mode = readMode();
    var current = null; // { kind, stop() }
    var retries = 0;
    var running = false;
    var retryTimer = null;
    var frameTimer = null;
    var ticketTimer = null;

    // ── Утилиты ─────────────────────────────────────────────────────────────
    function readMode() {
      try {
        return window.sessionStorage.getItem(storageKey) || "auto";
      } catch (e) {
        return "auto"; // приватный режим — просто работаем без запоминания
      }
    }

    function saveMode(value) {
      try {
        window.sessionStorage.setItem(storageKey, value);
      } catch (e) {
        /* не критично */
      }
    }

    function setStatus(text) {
      if (!statusEl) return;
      statusEl.textContent = text || "";
      statusEl.hidden = !text;
      // Пока что-то не так, панель показываем без наведения мыши: именно в этот
      // момент зрителю нужны кнопки переключения транспорта.
      root.classList.toggle("has-status", Boolean(text));
    }

    function setTransport(name) {
      if (transportEl) transportEl.textContent = name || "";
    }

    function markMode() {
      Array.prototype.forEach.call(modeButtons, function (button) {
        var active = button.dataset.mode === mode;
        button.setAttribute("aria-pressed", active ? "true" : "false");
        button.classList.toggle("is-active", active);
      });
    }

    function backoff() {
      retries += 1;
      return Math.min(RETRY_BASE * retries, RETRY_MAX);
    }

    function hasPicture() {
      return video.videoWidth > 0 && video.videoHeight > 0;
    }

    /** Через FIRST_FRAME_TIMEOUT проверяем, появилось ли изображение. */
    function watchFirstFrame() {
      clearTimeout(frameTimer);
      if (hasPicture()) return;
      frameTimer = setTimeout(function () {
        if (running && !hasPicture()) setStatus(NO_FRAMES);
      }, FIRST_FRAME_TIMEOUT);
    }

    video.addEventListener("loadeddata", function () {
      clearTimeout(frameTimer);
      if (hasPicture()) setStatus("");
    });

    // ── Доступ оператора ────────────────────────────────────────────────────
    function fetchTicket() {
      if (!ticketUrl) return Promise.resolve();
      return fetch(ticketUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken(), Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store"
      })
        .then(function (response) {
          if (!response.ok) throw new Error("сервер ответил " + response.status);
          return response.json();
        })
        .then(function (data) {
          whepUrl = data.whep;
          hlsUrl = data.hls;
          wantAudio = Boolean(data.audio);
        });
    }

    function keepTicketFresh() {
      if (!ticketUrl) return;
      clearInterval(ticketTimer);
      ticketTimer = setInterval(function () {
        if (!running) return;
        fetchTicket().catch(function () {
          /* следующая попытка через интервал; поток пока идёт */
        });
      }, TICKET_REFRESH);
    }

    // ── WebRTC ──────────────────────────────────────────────────────────────
    function waitIceGathering(pc) {
      if (pc.iceGatheringState === "complete") return Promise.resolve();
      return new Promise(function (resolve) {
        var timer = setTimeout(finish, ICE_GATHER_TIMEOUT);
        function finish() {
          clearTimeout(timer);
          pc.removeEventListener("icegatheringstatechange", onChange);
          resolve();
        }
        function onChange() {
          if (pc.iceGatheringState === "complete") finish();
        }
        pc.addEventListener("icegatheringstatechange", onChange);
      });
    }

    function startWebrtc() {
      // Кандидаты собираем полностью до отправки offer: так не нужен trickle-ICE
      // и лишний PATCH-раунд к серверу.
      var pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: "max-bundle" });
      var stream = new MediaStream();

      pc.addTransceiver("video", { direction: "recvonly" });
      if (wantAudio) pc.addTransceiver("audio", { direction: "recvonly" });

      pc.ontrack = function (event) {
        stream.addTrack(event.track);
        video.srcObject = stream;
      };

      return pc
        .createOffer()
        .then(function (offer) {
          return pc.setLocalDescription(offer);
        })
        .then(function () {
          return waitIceGathering(pc);
        })
        .then(function () {
          return fetch(whepUrl, {
            method: "POST",
            headers: { "Content-Type": "application/sdp" },
            body: pc.localDescription.sdp,
            credentials: "same-origin",
            cache: "no-store"
          });
        })
        .then(function (response) {
          if (response.status === 403) throw new Error(DENIED);
          if (response.status === 404) throw new Error("поток не найден на сервере");
          if (!response.ok) throw new Error("сервер ответил " + response.status);
          var location = response.headers.get("Location");
          return response.text().then(function (sdp) {
            return pc.setRemoteDescription({ type: "answer", sdp: sdp }).then(function () {
              return location;
            });
          });
        })
        .then(function (location) {
          return new Promise(function (resolve, reject) {
            var timer = setTimeout(function () {
              reject(new Error("ICE не установился — вероятно, в сети закрыт UDP"));
            }, WEBRTC_CONNECT_TIMEOUT);

            pc.addEventListener("connectionstatechange", function () {
              if (pc.connectionState === "connected") {
                clearTimeout(timer);
                resolve(makeWebrtcSession(pc, location));
              } else if (pc.connectionState === "failed") {
                clearTimeout(timer);
                reject(new Error("соединение не установлено"));
              }
            });
          });
        })
        .catch(function (error) {
          try {
            pc.close();
          } catch (e) {
            /* уже закрыт */
          }
          throw error;
        });
    }

    function makeWebrtcSession(pc, location) {
      var session = {
        kind: "WebRTC",
        stop: function () {
          try {
            pc.close();
          } catch (e) {
            /* игнорируем */
          }
          video.srcObject = null;
          if (location) {
            // Сообщаем серверу, что сессия больше не нужна, — не держим поток
            // с камеры ради закрытой вкладки.
            fetch(location, {
              method: "DELETE",
              credentials: "same-origin",
              keepalive: true
            }).catch(function () {});
          }
        }
      };

      pc.addEventListener("connectionstatechange", function () {
        if (session !== current) return;
        if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
          scheduleRetry("Соединение прервано");
        }
      });
      return session;
    }

    // ── HLS ─────────────────────────────────────────────────────────────────
    function loadHlsLibrary() {
      if (window.Hls) return Promise.resolve(true);
      return new Promise(function (resolve) {
        var script = document.createElement("script");
        script.src = "/static/vendor/hls.min.js";
        script.onload = function () {
          resolve(Boolean(window.Hls));
        };
        script.onerror = function () {
          resolve(false);
        };
        document.head.appendChild(script);
      });
    }

    function startHls() {
      video.srcObject = null;

      // Safari и iOS играют HLS нативно — библиотека там не нужна.
      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        return nativeHls();
      }

      return loadHlsLibrary().then(function (available) {
        if (!available) {
          throw new Error(
            "библиотека hls.js не установлена на сервере (bash ops/fetch-vendor.sh)"
          );
        }
        if (!window.Hls.isSupported()) {
          throw new Error("браузер не поддерживает HLS");
        }

        var hls = new window.Hls({ lowLatencyMode: true, backBufferLength: 10 });
        var session = {
          kind: "HLS",
          stop: function () {
            try {
              hls.destroy();
            } catch (e) {
              /* уже уничтожен */
            }
          }
        };

        // Успех — это разобранный манифест, а не подключённая библиотека.
        // Раньше startHls резолвился сразу, и плеер писал «HLS» даже когда
        // сервер отвечал 403 или редиректом: транспорт выглядел выбранным,
        // а картинки не было — и это сбивало с толку при разборе.
        return new Promise(function (resolve, reject) {
          var settled = false;

          hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
            if (settled) return;
            settled = true;
            resolve(session);
          });

          hls.on(window.Hls.Events.ERROR, function (_event, data) {
            if (!data || !data.fatal) return;
            var message = hlsErrorText(data);
            if (!settled) {
              settled = true;
              session.stop();
              reject(new Error(message));
            } else if (session === current) {
              scheduleRetry(message);
            }
          });

          hls.loadSource(hlsUrl);
          hls.attachMedia(video);
        });
      });
    }

    function nativeHls() {
      return new Promise(function (resolve, reject) {
        function cleanup() {
          video.removeEventListener("loadedmetadata", onReady);
          video.removeEventListener("error", onError);
        }
        function onReady() {
          cleanup();
          resolve({
            kind: "HLS",
            stop: function () {
              video.removeAttribute("src");
              video.load();
            }
          });
        }
        function onError() {
          cleanup();
          reject(new Error("браузер не смог открыть плейлист"));
        }
        video.addEventListener("loadedmetadata", onReady);
        video.addEventListener("error", onError);
        video.src = hlsUrl;
      });
    }

    function hlsErrorText(data) {
      var status = (data.response && data.response.code) || 0;
      if (status === 403) return "HLS: " + DENIED;
      if (status === 404) return "HLS: поток не поднят на сервере";
      return "HLS: " + (data.details || "ошибка") + (status ? " (HTTP " + status + ")" : "");
    }

    // ── Общий цикл подключения ──────────────────────────────────────────────
    function teardown() {
      clearTimeout(retryTimer);
      clearTimeout(frameTimer);
      if (current) {
        var session = current;
        current = null;
        session.stop();
      }
    }

    function scheduleRetry(reason) {
      if (!running) return;
      teardown();
      var delay = backoff();
      setStatus(reason + ". Повтор через " + Math.round(delay / 1000) + " с…");
      retryTimer = setTimeout(connect, delay);
    }

    function succeed(session) {
      current = session;
      retries = 0;
      setTransport(session.kind);
      setStatus("");
      video.play().catch(function () {});
      watchFirstFrame();
    }

    function connect() {
      if (!running) return;
      teardown();
      setStatus("Подключение…");

      if (mode === "hls") {
        startHls()
          .then(succeed)
          .catch(function (error) {
            scheduleRetry("HLS недоступен: " + error.message);
          });
        return;
      }

      startWebrtc()
        .then(succeed)
        .catch(function (error) {
          if (!running) return;
          if (mode === "webrtc") {
            scheduleRetry("WebRTC недоступен: " + error.message);
            return;
          }
          // Режим «Авто»: UDP закрыт или камера не отвечает — пробуем HLS.
          setStatus("WebRTC недоступен (" + error.message + "), перехожу на HLS…");
          startHls()
            .then(succeed)
            .catch(function (hlsError) {
              scheduleRetry("Ни один транспорт не заработал: " + hlsError.message);
            });
        });
    }

    // ── Управление ──────────────────────────────────────────────────────────
    function start() {
      if (running) return;
      running = true;
      retries = 0;
      root.classList.add("is-playing");
      if (posterEl) posterEl.hidden = true;
      if (stopBtn) stopBtn.hidden = false;
      setStatus("Подключение…");

      fetchTicket()
        .then(function () {
          keepTicketFresh();
          connect();
        })
        .catch(function (error) {
          running = false;
          root.classList.remove("is-playing");
          if (posterEl) posterEl.hidden = false;
          if (stopBtn) stopBtn.hidden = true;
          setStatus("Не удалось получить доступ к камере: " + error.message);
        });
    }

    function stop() {
      running = false;
      clearInterval(ticketTimer);
      teardown();
      root.classList.remove("is-playing");
      setTransport("");
      setStatus("");
      video.removeAttribute("src");
      video.srcObject = null;
      if (stopBtn) stopBtn.hidden = true;
      if (posterEl) posterEl.hidden = false;
    }

    Array.prototype.forEach.call(modeButtons, function (button) {
      button.addEventListener("click", function () {
        if (mode === button.dataset.mode) return;
        mode = button.dataset.mode;
        saveMode(mode);
        markMode();
        retries = 0;
        setTransport("");
        if (running) connect();
      });
    });

    if (posterEl) posterEl.addEventListener("click", start);
    if (stopBtn) stopBtn.addEventListener("click", stop);

    if (soundBtn) {
      soundBtn.hidden = !wantAudio;
      soundBtn.addEventListener("click", function () {
        video.muted = !video.muted;
        soundBtn.textContent = video.muted ? "Включить звук" : "Выключить звук";
        if (!video.muted) video.play().catch(function () {});
      });
    }

    if (fullscreenBtn) {
      fullscreenBtn.addEventListener("click", function () {
        if (document.fullscreenElement) {
          document.exitFullscreen();
        } else if (root.requestFullscreen) {
          root.requestFullscreen();
        }
      });
    }

    markMode();
    return { start: start, stop: stop, root: root };
  }

  var players = [];

  function mountAll(scope) {
    var nodes = (scope || document).querySelectorAll(".player");
    Array.prototype.forEach.call(nodes, function (root) {
      if (root.dataset.mounted === "1") return;
      root.dataset.mounted = "1";
      var player = create(root);
      if (!player) return;
      players.push(player);
      if (root.dataset.autostart === "1") player.start();
    });
  }

  window.RTSPPlayer = {
    mount: mountAll,
    get: function (root) {
      for (var i = 0; i < players.length; i += 1) {
        if (players[i].root === root) return players[i];
      }
      return null;
    }
  };

  // Уходя со страницы, гасим все потоки: незакрытая WebRTC-сессия держит
  // камеру подключённой ещё несколько минут.
  window.addEventListener("pagehide", function () {
    players.forEach(function (player) {
      player.stop();
    });
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      mountAll(document);
    });
  } else {
    mountAll(document);
  }
})();
