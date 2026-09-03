# RTSP Gateway

Превращает RTSP-камеры в публичные `https://`-ссылки, которые открываются
в браузере без плагинов. Корпоративный аналог rtsp.cam, разворачиваемый
у себя.

* Вход в панель по логину и паролю, второй фактор — TOTP.
* Ссылка на просмотр — с ограниченным сроком действия и мгновенным отзывом.
* Учётные данные камер и их реальные адреса зрителю не видны никогда.
* WebRTC (задержка около секунды) с автоматическим запасным вариантом
  LL-HLS для сетей, где закрыт UDP; транспорт можно зафиксировать кнопкой
  прямо в плеере.
* Предпросмотр до сохранения: кнопка «Проверить и показать кадр» подключается
  к камере, снимает кадр и говорит, покажет ли этот поток браузер.
* Пошаговая диагностика камеры по кнопке: сеть, RTSP-ответ, кодеки, кадр,
  совместимость с браузером и — главное — реальная попытка медиа-сервера
  забрать поток, с ответом, что именно чинить.
* **Домен не обязателен**: можно поднять на голом IP — через sslip.io
  с настоящим сертификатом Let's Encrypt либо со своим CA для внутренней сети.

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
портов открыт только `8189` (udp и tcp) для медиа WebRTC.

Подробнее: [docs/security.md](docs/security.md).

## Быстрый старт

На чистом сервере достаточно одной команды — установщик спросит домен, почту
и остальное, поставит Docker, сгенерирует секреты и поднимет всё сам:

```bash
sudo bash install.sh
```

Повторный запуск безопасен: существующий `.env` можно оставить, а ключ
шифрования не перезаписывается никогда.

Если хочется руками:

```bash
cp .env.example .env && $EDITOR .env
```

```bash
openssl rand -base64 32 > secrets/app_key
chown 10001:10001 secrets/app_key && chmod 400 secrets/app_key
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

Установщик тоже покрыт тестами — они проверяют разбор ввода и генерацию `.env`
без сервера и без docker:

```bash
bash tests/test_install_sh.sh
```

## Состав

| Путь | Что |
|---|---|
| `install.sh` | интерактивная установка на чистый сервер |
| `uninstall.sh` | удаление тремя уровнями — для повторных прогонов при тестах |
| `docker-compose.yml` | caddy, mediamtx, api, worker, postgres, redis |
| `Caddyfile` | TLS, forward_auth, маршрутизация медиа |
| `mediamtx/mediamtx.yml` | глобальные настройки медиа-сервера (пути — динамические) |
| `backend/app/media/` | клиент Control API, реконсилятор, проба кодеков, диагностика, SSRF-проверки |
| `backend/app/auth/` | пароли, TOTP, сессии, CSRF, лимиты частоты |
| `backend/app/internal/authz.py` | эндпоинт forward_auth |
| `backend/app/web/` | панель и плеер (Jinja2, без SPA) |
| `ops/` | бэкап, восстановление, Prometheus |
