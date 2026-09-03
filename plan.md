# RTSP → HTTPS: корпоративный аналог rtsp.cam

## Context

Есть камеры с проброшенными портами и RTSP-ссылками вида `rtsp://user:pass@31.148.246.249:4259`. Нужен внутренний сервис, который:

- принимает такую ссылку через веб-панель (вход по логину/паролю + опциональный TOTP-2FA);
- отдаёт наружу публичную `https://`-ссылку на просмотр в браузере без плагинов;
- не раскрывает учётные данные камеры и её реальный IP:порт зрителю;
- работает стабильно и переживает падение любого компонента без ручного вмешательства.

Каталог `C:\Users\v.baymanov\Documents\rtsp_server_par` пуст — проект пишется с нуля.

**Решения, зафиксированные с пользователем:**

| Вопрос | Выбор |
|---|---|
| Транспорт | WebRTC (WHEP) основной + LL-HLS фолбэк |
| Публичные ссылки | Подписанный токен с TTL + возможность отзыва |
| Инфраструктура | Один VPS, Docker Compose (архитектура готова к 2+ узлам) |
| Бэкенд | Python (FastAPI) |

**Не-цель первой версии:** запись/архив, PTZ, детекция движения, мультитенантность с биллингом. Схема БД закладывается так, чтобы запись добавилась без миграции ломающего типа.

---

## Архитектура

```
Камера ──RTSP/TCP──▶ MediaMTX ──WHEP/LL-HLS──▶ Caddy ──HTTPS──▶ Браузер
   (pull, on-demand)   (127.0.0.1)    ▲ forward_auth │
                            ▲          └─────────────┤
                   Control API (9997) ▲              │
                            │         │              │
                     FastAPI control-plane ──────────┘
                            │
                   PostgreSQL + Redis
```

**Ключевой принцип:** MediaMTX слушает только на `127.0.0.1`. Наружу смотрит один Caddy, который для каждого медиа-запроса дёргает `forward_auth` в FastAPI. RTSP/RTMP/SRT-серверы в MediaMTX выключены полностью — мы только тянем поток с камер, ничего не принимаем. Единственный публичный порт кроме 443 — `8189/udp` для WebRTC-медиа.

### Компоненты (docker-compose)

| Сервис | Образ | Порты наружу | Роль |
|---|---|---|---|
| `caddy` | `caddy:2-alpine` | 80, 443 | TLS (Let's Encrypt), forward_auth, реверс-прокси, security-заголовки |
| `mediamtx` | `bluenviron/mediamtx:latest-ffmpeg` | 8189/udp | Пул RTSP с камер, отдача WHEP + LL-HLS |
| `api` | свой (python:3.12-slim) | — | FastAPI: панель, авторизация, ссылки, authz-хук, реконсилятор |
| `worker` | тот же образ | — | ffprobe-проба камер, снапшоты-превью, чистка токенов |
| `postgres` | `postgres:16-alpine` | — | Пользователи, камеры, ссылки, аудит |
| `redis` | `redis:7-alpine` | — | Сессии, rate-limit, кэш authz-решений |

Всё, кроме caddy и mediamtx, — во внутренней docker-сети без публикации портов. Контейнеры: `read_only: true`, `cap_drop: [ALL]`, non-root user, `no-new-privileges`, healthcheck + `restart: unless-stopped`.

### Поток «добавили камеру»

1. Оператор вводит RTSP-URL в панели.
2. Бэкенд **валидирует хост**: DNS-резолв + запрет приватных/link-local/loopback диапазонов (защита от SSRF и от разведки внутренней сети через сервис). Исключения — только через явный allowlist в конфиге.
3. Креды шифруются (libsodium SecretBox, ключ из `SECRET_KEY_FILE`, монтируется как docker secret) и кладутся в БД. Наружу из API они больше **никогда** не возвращаются — поле write-only.
4. `worker` делает `ffprobe` через RTSP/TCP: видеокодек, аудиокодек, разрешение, fps. Результат — в карточку камеры:
   - H.264 + (Opus | без звука) → работает нативно, транскод не нужен;
   - H.265 / MJPEG → WebRTC в браузере не сыграет, включаем профиль транскода;
   - аудио PCMA/PCMU/AAC → перекодировать в Opus либо выключить звук.
5. Реконсилятор пушит путь в MediaMTX через Control API `POST /v3/config/paths/add/{name}`.

### Профиль потока (per-camera)

- **passthrough** (по умолчанию, 0% CPU) — `source: rtsp://…`, `rtspTransport: tcp`, `sourceOnDemand: yes`, `sourceOnDemandCloseAfter: 60s`. Поток с камеры тянется только пока есть зритель.
- **transcode** — для H.265/несовместимого аудио: путь с `runOnDemand`, запускающим ffmpeg-сайдкар (`-c:v libx264 -preset veryfast -tune zerolatency -c:a libopus`), публикующий во внутренний путь. Явно помечается в UI как «нагружает CPU»: примерно 1 ядро на 1080p@15fps.
- **always-on** — флаг для камер, где важен мгновенный старт: `sourceOnDemand: no`.

### Реконсилятор (сердце отказоустойчивости)

Фоновая задача в `api`, каждые 15 с:

1. `GET /v3/config/paths/list` и `GET /v3/paths/list` из MediaMTX;
2. diff с состоянием в Postgres (источник истины);
3. add/patch/delete недостающего и лишнего;
4. запись статуса каждого пути (`ready`, `ready_time`, `bytes_received`, ошибка источника) в БД → в UI видно «камера онлайн/оффлайн/ошибка авторизации».

Это делает перезапуск MediaMTX бесшовным: конфиг восстанавливается сам, а не живёт в файле. `mediamtx.yml` содержит только глобальные настройки, пути — целиком динамические.

### Авторизация просмотра

Ссылка на просмотр: `https://cam.company.ru/v/<slug>?t=<token>`

- `token` — **непрозрачные 32 случайных байта** (base64url), в БД хранится только `sha256(token)`. Не JWT — отзыв мгновенный и без списков отзыва.
- В БД: `expires_at` (час / день / неделя / бессрочно), `revoked_at`, `max_concurrent`, опционально `allowed_ips` (CIDR) и `password_hash` для ссылки.
- В Caddy на `/v/*`, `/embed/*`, `/whep/*`, `/hls/*` стоит `forward_auth` → `POST /internal/authz` в FastAPI, куда прокидывается оригинальный URI, IP клиента, Referer, cookie.
- FastAPI проверяет токен, TTL, отзыв, IP, лимит одновременных сессий → 200 (+ заголовок `X-Mtx-Path` с внутренним именем потока) либо 403.
- Решение кэшируется в Redis на 20 с по ключу `sha256(token)` — иначе на каждый HLS-сегмент будет запрос в БД. Отзыв ссылки удаляет ключ немедленно, поэтому реальная задержка отзыва — 0.
- Плеер-страница на первом заходе ставит короткоживущую cookie `mtx_sid` (HttpOnly, Secure, SameSite=Lax), чтобы сегменты HLS и WHEP-PATCH не таскали токен в каждом URL.

**Почему не `authMethod: http` в самом MediaMTX:** он не видит cookie и не даёт управлять лимитами/аудитом, а его ответ нельзя кэшировать. Caddy `forward_auth` покрывает всё и оставляет MediaMTX недоступным снаружи в принципе. В `mediamtx.yml` при этом всё равно ставим `authInternalUsers`, разрешающих доступ только с `127.0.0.1` — второй рубеж на случай ошибки в сети docker.

### Плеер

Одна страница, отдаёт `<video>` + логика:
1. пробуем WHEP (`POST /whep/<path>`, ICE, `8189/udp`);
2. при неудаче ICE за 5 с — падаем на LL-HLS через hls.js (`/hls/<path>/index.m3u8`);
3. Safari/iOS — сразу нативный HLS.

Отдаём три формата ссылки: обычная страница, `iframe`-embed (`/embed/<slug>?t=…`), и прямой `.m3u8` для VLC/сторонних плееров.

**TURN:** на первом этапе не нужен — у VPS публичный IP, `webrtcAdditionalHosts: [<public-ip или домен>]` + открытый `8189/udp` покрывает ~95% сетей. Для корпоративных сетей с блокировкой UDP есть HLS-фолбэк. Если понадобится — добавляем coturn отдельным сервисом и прописываем в `webrtcICEServers2`.

---

## Безопасность панели

| Область | Решение |
|---|---|
| Пароли | argon2id (`argon2-cffi`), политика длины ≥ 12, проверка по списку утёкших (k-anonymity HIBP, опционально offline-список) |
| 2FA | TOTP (`pyotp`), QR при включении, 10 одноразовых recovery-кодов (хранятся хешированными), окно ±1 шаг, защита от replay (запоминаем использованный step в Redis) |
| Политика 2FA | Флаг в настройках: «обязательна для всех» / «обязательна для админов» / опциональна |
| Сессии | Серверные, в Redis; cookie HttpOnly + Secure + SameSite=Lax; ротация ID при логине и при включении 2FA; список активных сессий с кнопкой «завершить» |
| CSRF | Double-submit токен на все мутирующие запросы |
| Брутфорс | Rate-limit по IP и по логину (Redis), экспоненциальная задержка, блокировка на 15 мин после 10 неудач, уведомление админу |
| Роли | `admin` (пользователи, настройки, все камеры) / `operator` (свои камеры и ссылки) / `viewer` (только просмотр) |
| Аудит | Неизменяемая таблица: вход, неудачный вход, включение/выключение 2FA, создание/удаление камеры и ссылки, просмотр по публичной ссылке (IP, UA, длительность) |
| Заголовки | HSTS (preload после проверки), CSP с nonce и без `unsafe-inline`, `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `frame-ancestors` — только для `/embed/*` разрешаем встраивание |
| Секреты камер | libsodium SecretBox, ключ в docker secret, ротация через `python -m app.cli rotate-key` |
| SSRF | Блок приватных диапазонов при добавлении камеры (см. выше) |
| Образы | Пиннинг по digest, `pip-audit` + Trivy в CI, автосборка при обновлении базового образа |
| Панель | Опционально ограничить `/admin` по IP-allowlist на уровне Caddy |

---

## Отказоустойчивость на одном VPS

1. **Healthcheck'и + restart policy** на каждый контейнер; MediaMTX проверяется через `GET /v3/paths/list`.
2. **Реконсилятор** возвращает конфиг после любого рестарта MediaMTX — состояние не теряется.
3. **Watchdog по потокам**: если путь `ready: false` дольше 60 с — `patch` пути (переподключение), после 3 попыток — пометка «камера недоступна» + алерт. Экспоненциальный backoff, чтобы не долбить мёртвую камеру.
4. **Бэкапы**: `pg_dump` по cron в `/var/backups` + выгрузка в S3/минио, ежедневно, retention 30 дней; `ops/restore.sh` с проверенной процедурой восстановления.
5. **Мониторинг**: MediaMTX `metrics: yes` (:9998) + `/metrics` FastAPI → Prometheus + Grafana + Alertmanager (Telegram/почта). Алерты: камера оффлайн > 5 мин, CPU > 85%, диск > 80%, 5xx на панели, истекает TLS-сертификат.
6. **Логи**: структурный JSON, ротация, отдельный поток аудита.
7. **Апгрейд до 2 узлов** (когда понадобится): вынести Postgres/Redis на отдельный узел, поднять второй `mediamtx`+`api`, добавить HAProxy/keepalived с плавающим IP, распределять камеры по узлам через колонку `node_id` + аренду в Redis. Схема БД и реконсилятор уже написаны с учётом `node_id` — переезд не потребует переписывания.

---

## Структура репозитория

```
rtsp_server_par/
├─ docker-compose.yml
├─ docker-compose.prod.yml
├─ .env.example
├─ Caddyfile
├─ mediamtx/mediamtx.yml            # только глобальные настройки, пути — динамические
├─ backend/
│  ├─ Dockerfile
│  ├─ pyproject.toml
│  └─ app/
│     ├─ main.py  config.py  db.py  models.py  schemas.py  crypto.py
│     ├─ auth/       passwords.py  totp.py  sessions.py  csrf.py  ratelimit.py  deps.py
│     ├─ api/        auth.py  cameras.py  links.py  users.py  audit.py  settings.py
│     ├─ internal/   authz.py            # эндпоинт для Caddy forward_auth
│     ├─ media/      mtx_client.py  reconciler.py  watchdog.py  probe.py  snapshot.py  ssrf.py
│     ├─ web/        templates/ (Jinja2 + htmx)  static/ (player.js, whep.js, hls.js)
│     └─ cli.py                          # create-admin, rotate-key, revoke-link
│  ├─ alembic/
│  └─ tests/
├─ ops/  backup.sh  restore.sh  prometheus/  grafana/
└─ docs/  install.md  runbook.md  security.md
```

**Фронтенд — Jinja2 + htmx, а не SPA.** Один деплой, нет отдельного npm-дерева зависимостей, CSP без `unsafe-eval`, меньше поверхность атаки. Сложная интерактивность здесь не нужна: список камер, форма, кнопка «создать ссылку». Только плеер — обычный JS (hls.js + минимальный WHEP-клиент ~100 строк).

## Схема БД (основное)

- `users` — email, argon2-хеш, роль, `totp_secret_enc`, `totp_enabled`, `must_change_password`, `failed_attempts`, `locked_until`
- `recovery_codes` — `user_id`, `code_hash`, `used_at`
- `cameras` — `name`, `rtsp_url_enc`, `host`, `port`, `mtx_path` (случайный, не угадываемый), `profile` (passthrough/transcode), `on_demand`, `node_id`, `probe_result` (jsonb), `status`, `last_ready_at`
- `share_links` — `camera_id`, `slug`, `token_hash`, `expires_at`, `revoked_at`, `max_concurrent`, `allowed_cidrs`, `password_hash`, `created_by`
- `view_sessions` — `link_id`, `ip`, `ua`, `started_at`, `ended_at`, `bytes`
- `audit_log` — `actor_id`, `action`, `target`, `ip`, `meta` (jsonb), `created_at` (только INSERT, права роли БД это ограничивают)

---

## Этапы

| # | Что | Результат / критерий готовности |
|---|---|---|
| **M0** | Скелет: compose, Caddy с локальным TLS, MediaMTX на localhost, FastAPI `/healthz`, Postgres+Redis, alembic | `docker compose up` поднимает всё, `/healthz` зелёный |
| **M1** | Захардкоженный путь на реальную камеру пользователя, страница плеера с WHEP + HLS-фолбэком | Видео из `rtsp://…@31.148.246.249:4259` играет в Chrome и Safari |
| **M2** | Авторизация: пользователи, argon2, сессии в Redis, CSRF, rate-limit, `cli create-admin`, аудит | Вход/выход работает, брутфорс блокируется |
| **M3** | 2FA: TOTP, QR, recovery-коды, политика обязательности | Вход с Google Authenticator, восстановление по коду |
| **M4** | CRUD камер: SSRF-валидация, шифрование кредов, ffprobe-проба, реконсилятор, статусы | Камера добавляется из UI и сразу появляется в MediaMTX; удаление чистит путь |
| **M5** | Публичные ссылки: генерация токена, TTL, отзыв, `forward_auth`, кэш в Redis, embed + `.m3u8` | Ссылка играет у стороннего человека; после «Отозвать» — мгновенно 403 |
| **M6** | Отказоустойчивость: watchdog, healthcheck'и, бэкапы, Prometheus+Grafana+алерты, снапшоты-превью | Убийство любого контейнера → самовосстановление ≤ 60 с; алерт при оффлайн-камере |
| **M7** | Продакшн: реальный домен + Let's Encrypt, UFW, fail2ban, security-заголовки, `docs/runbook.md`, нагрузочный тест | Оценка A на securityheaders.com, пройден чеклист из `docs/security.md` |

Профиль транскода (H.265/аудио) делаем в M4, но включаем по флагу — если все камеры отдают H.264+G.711, можно отложить.

---

## Проверка

**Функциональная (после каждой вехи):**

```bash
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
```

```bash
docker compose exec api python -m app.cli create-admin --email admin@company.ru
```

```bash
curl -sk https://localhost/healthz && curl -s http://127.0.0.1:9997/v3/paths/list | jq '.items[] | {name, ready, tracks}'
```

**Медиа:**
- WHEP: открыть `/v/<slug>?t=<token>` в Chrome, в `chrome://webrtc-internals` убедиться, что ICE в состоянии `connected` и растёт `framesDecoded`.
- HLS: `ffplay "https://cam.company.ru/hls/<path>/index.m3u8"` с валидной cookie/токеном; без токена — 403.
- On-demand: закрыть все вкладки → через 60 с `bytes_received` перестаёт расти, соединение с камерой закрыто (`docker compose logs mediamtx`).

**Безопасность (обязательный чеклист перед продом):**
- прямой запрос на `http://<vps-ip>:8888/` и `:8889/` — connection refused (MediaMTX не слушает наружу);
- `/hls/...` и `/whep/...` без токена → 403; с истёкшим токеном → 403; с отозванным → 403 в пределах секунды;
- в HTML/JSON ответах панели нигде нет `rtsp://` с паролем — проверить `grep -r 'rtsp://' ` по ответам API;
- попытка добавить камеру `rtsp://10.0.0.1/`, `rtsp://127.0.0.1/`, `rtsp://169.254.169.254/` → отказ;
- вход без 2FA при включённой политике невозможен; повторное использование того же TOTP-кода отклоняется;
- `nmap -sS -sU <vps-ip>` → открыты только 80, 443, 8189/udp;
- ZAP baseline scan по панели без High-находок.

**Автотесты:** pytest — юнит на crypto/totp/token-lifecycle/SSRF-валидатор, интеграционные на authz-эндпоинт и реконсилятор (MediaMTX в testcontainer), в CI + `pip-audit` и Trivy.

**Нагрузка:** 20 одновременных HLS-зрителей через `ffmpeg -re` + 5 WebRTC-сессий; смотрим CPU, память MediaMTX и p95 времени ответа `/internal/authz`.

---

## Риски

| Риск | Митигация |
|---|---|
| Камера отдаёт H.265 — WebRTC не сыграет | Проба на этапе добавления, явное предупреждение в UI, профиль транскода |
| Аудио G.711/AAC не идёт в WebRTC | Транскод в Opus либо отключение звука (флаг у камеры) |
| Корпоративная сеть зрителя режет UDP | HLS-фолбэк; при массовой проблеме — coturn с TURN over TCP/443 |
| Один VPS = единая точка отказа | Заложен `node_id` и stateless-конфиг MediaMTX; переход на 2 узла без переписывания |
| Публичная ссылка утечёт | TTL по умолчанию (сутки, не «бессрочно»), отзыв в один клик, лимит одновременных сессий, аудит просмотров, опциональный IP-allowlist |
| Канал VPS — узкое место | 1080p@2Mbps × N зрителей; считать заранее, on-demand экономит только исходящий с камеры, не к зрителям |
