/*
 * Плеер публичной ссылки.
 *
 * Порядок: WebRTC (WHEP) -> LL-HLS. WebRTC даёт задержку около секунды, но
 * требует UDP; в корпоративных сетях, где UDP закрыт, автоматически включается
 * HLS через тот же HTTPS-порт.
 *
 * Токен ссылки здесь не участвует: доступ уже выдан cookie, которую поставила
 * страница просмотра, а Caddy проверяет её через forward_auth на каждый запрос.
 */
(function () {
  "use strict";

  var root = document.getElementById("player");
  var video = document.getElementById("video");
  var statusEl = document.getElementById("status");
  var transportEl = document.getElementById("transport");
  var soundBtn = document.getElementById("sound");
  var fullscreenBtn = document.getElementById("fullscreen");

  if (!root || !video) return;

  var whepUrl = root.dataset.whep;
  var hlsUrl = root.dataset.hls;
  var wantAudio = root.dataset.audio === "1";

  var ICE_GATHER_TIMEOUT = 3000;
  var WEBRTC_CONNECT_TIMEOUT = 8000;
  var RETRY_BASE = 2000;
  var RETRY_MAX = 30000;

  var session = null;
  var retries = 0;
  var stopped = false;

  function setStatus(text) {
    if (!statusEl) return;
    if (text) {
      statusEl.textContent = text;
      statusEl.hidden = false;
    } else {
      statusEl.hidden = true;
    }
  }

  function setTransport(name) {
    if (transportEl) transportEl.textContent = name;
  }

  function backoff() {
    retries += 1;
    return Math.min(RETRY_BASE * retries, RETRY_MAX);
  }

  // ── WebRTC ────────────────────────────────────────────────────────────────
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
        if (!response.ok) throw new Error("WHEP " + response.status);
        var location = response.headers.get("Location");
        return response.text().then(function (sdp) {
          return pc.setRemoteDescription({ type: "answer", sdp: sdp }).then(function () {
            return { pc: pc, location: location };
          });
        });
      })
      .then(function (created) {
        return new Promise(function (resolve, reject) {
          var timer = setTimeout(function () {
            reject(new Error("ICE не установился"));
          }, WEBRTC_CONNECT_TIMEOUT);

          pc.addEventListener("connectionstatechange", function () {
            if (pc.connectionState === "connected") {
              clearTimeout(timer);
              resolve(created);
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

  function closeSession() {
    if (!session) return;
    var current = session;
    session = null;
    try {
      current.pc.close();
    } catch (e) {
      /* игнорируем */
    }
    if (current.location) {
      // Сообщаем серверу, что сессия больше не нужна, — не держим поток
      // с камеры ради закрытой вкладки.
      fetch(current.location, {
        method: "DELETE",
        credentials: "same-origin",
        keepalive: true
      }).catch(function () {});
    }
  }

  // ── HLS ───────────────────────────────────────────────────────────────────
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
    // Safari и iOS играют HLS нативно — библиотека там не нужна.
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = hlsUrl;
      setTransport("HLS");
      setStatus("");
      return Promise.resolve(true);
    }
    return loadHlsLibrary().then(function (available) {
      if (!available || !window.Hls.isSupported()) return false;
      var hls = new window.Hls({ lowLatencyMode: true, backBufferLength: 10 });
      hls.on(window.Hls.Events.ERROR, function (_event, data) {
        if (data && data.fatal) {
          hls.destroy();
          scheduleRetry("поток недоступен");
        }
      });
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
      setTransport("HLS");
      setStatus("");
      return true;
    });
  }

  // ── Общий цикл подключения ────────────────────────────────────────────────
  function scheduleRetry(reason) {
    if (stopped) return;
    var delay = backoff();
    setStatus(reason + ". Повтор через " + Math.round(delay / 1000) + " с…");
    setTimeout(connect, delay);
  }

  function connect() {
    if (stopped) return;
    setStatus("Подключение…");

    startWebrtc()
      .then(function (created) {
        session = created;
        retries = 0;
        setTransport("WebRTC");
        setStatus("");
        created.pc.addEventListener("connectionstatechange", function () {
          if (
            created.pc.connectionState === "failed" ||
            created.pc.connectionState === "disconnected"
          ) {
            closeSession();
            scheduleRetry("Соединение прервано");
          }
        });
      })
      .catch(function () {
        // UDP закрыт или камера не отвечает — пробуем HLS.
        setStatus("WebRTC недоступен, переключаюсь на HLS…");
        startHls().then(function (started) {
          if (!started) {
            scheduleRetry("Браузер не смог воспроизвести поток");
          }
        });
      });
  }

  // ── Управление ────────────────────────────────────────────────────────────
  if (soundBtn) {
    if (wantAudio) soundBtn.hidden = false;
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

  window.addEventListener("pagehide", function () {
    stopped = true;
    closeSession();
  });

  connect();
})();
