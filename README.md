# RTSP Gateway

Превращает RTSP-камеры в публичные `https://`-ссылки, которые открываются
в браузере без плагинов. Корпоративный аналог rtsp.cam, разворачиваемый
у себя.

* Вход в панель по логину и паролю, второй фактор — TOTP.
* Ссылка на просмотр — с ограниченным сроком действия и мгновенным отзывом.
* Учётные данные камер и их реальные адреса зрителю не видны никогда.
* WebRTC (задержка около секунды) с автоматическим запасным вариантом
  LL-HLS для сетей, где закрыт UDP.

## Как это устроено

```
Камера ──RTSP/TCP──▶ MediaMTX ──WHEP/LL-HLS──▶ Caddy ──HTTPS──▶ Браузер
   (pull, on-demand)   (только    ▲ forward_auth │
                       внутри     └──────────────┤
                       docker)                   │
                            ▲ Control API        │
                            │                    │
                     FastAPI control-plane ──────┘
                            │
                   PostgreSQL + Redis
```

Два решения определяют всё остальное:

**MediaMTX не хранит своё состояние.** Список камер живёт в PostgreSQL,
а реконсилятор приводит медиа-сервер в соответствие каждые 15 секунд.
Перезапуск, падение или обновление MediaMTX не требуют ручных действий
и не теряют настройки.

**Ни один медиа-запрос не доходит до MediaMTX без проверки.** Caddy на
каждый запрос за плейлистом, сегментом или WebRTC-сессией спрашивает
разрешение у control-plane. Сам MediaMTX наружу не смотрит вообще: из
портов открыт только `8189/udp` для медиа.

Подробнее: [docs/security.md](docs/security.md).

## Быстрый старт

```bash
cp .env.example .env && $EDITOR .env
```

```bash
docker compose run --rm --no-deps api python -m app.cli gen-key > secrets/app_key
```

```bash
docker compose up -d --build && docker compose run --rm api alembic upgrade head
```

```bash
docker compose exec -it api python -m app.cli create-admin --email admin@company.ru
```

Полная инструкция — [docs/install.md](docs/install.md).
Эксплуатация и разбор типовых сбоев — [docs/runbook.md](docs/runbook.md).

## Разработка

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
cd backend && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check app tests
```

Тесты не требуют ни базы, ни Redis: Redis подменяется на in-memory, а схема
БД сверяется с миграциями офлайн, через рендер SQL.

## Состав

| Путь | Что |
|---|---|
| `docker-compose.yml` | caddy, mediamtx, api, worker, postgres, redis |
| `Caddyfile` | TLS, forward_auth, маршрутизация медиа |
| `mediamtx/mediamtx.yml` | глобальные настройки медиа-сервера (пути — динамические) |
| `backend/app/media/` | клиент Control API, реконсилятор, проба кодеков, SSRF-проверки |
| `backend/app/auth/` | пароли, TOTP, сессии, CSRF, лимиты частоты |
| `backend/app/internal/authz.py` | эндпоинт forward_auth |
| `backend/app/web/` | панель и плеер (Jinja2, без SPA) |
| `ops/` | бэкап, восстановление, Prometheus |
