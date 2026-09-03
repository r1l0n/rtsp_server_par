# Установка

Целевая платформа — один VPS с Linux, Docker и публичным IP.
Минимум: 2 vCPU, 4 ГБ RAM, 20 ГБ диска. Транскодирование добавляет примерно
одно ядро на камеру, поэтому при H.265-камерах считайте CPU отдельно.

## Быстрый путь: install.sh

На чистом сервере всё делается одной командой:

```bash
sudo bash install.sh
```

Скрипт спросит домен, почту для Let's Encrypt, политику 2FA и адрес
администратора; проверит, что домен резолвится на этот сервер; поставит
Docker из официального репозитория; сгенерирует пароль базы и ключ шифрования;
настроит файрвол (первым делом разрешив ваш текущий SSH-порт); соберёт образы,
накатит миграции и предложит задать пароль администратора.

Повторный запуск безопасен: существующий `.env` можно оставить, ключ
шифрования не перезаписывается никогда, а `docker compose up` идемпотентен.

Ниже — то же самое вручную, если нужен контроль над каждым шагом.

## 1. Подготовка сервера

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
```

### DNS

`A`-запись домена (например `cam.company.ru`) должна указывать на IP этого
сервера. Без этого Let's Encrypt не выдаст сертификат.

### Файрвол

Наружу открыто ровно три порта. Всё остальное — включая Control API MediaMTX,
Postgres, Redis и порты HLS/WebRTC-сигналинга — доступно только внутри
docker-сетей.

```bash
sudo ufw allow 22/tcp        # ssh
sudo ufw allow 80/tcp        # ACME-проверка и редирект на https
sudo ufw allow 443/tcp       # панель, плеер, медиа
sudo ufw allow 8189/udp      # медиа WebRTC
sudo ufw enable
```

## 2. Конфигурация

```bash
git clone <репозиторий> /opt/rtspgw && cd /opt/rtspgw
cp .env.example .env
```

Отредактируйте `.env`: домен, почта для Let's Encrypt, пароль Postgres.
Пароли удобно сгенерировать так:

```bash
openssl rand -base64 30
```

### Ключ шифрования

Учётные данные камер и TOTP-секреты лежат в базе зашифрованными. Ключ живёт
отдельным файлом и монтируется как docker secret.

```bash
docker compose run --rm --no-deps api python -m app.cli gen-key > secrets/app_key
chmod 600 secrets/app_key
```

**Сохраните копию ключа в менеджере паролей компании.** Без него дамп базы
бесполезен: камеры придётся заводить заново.

### hls.js (фолбэк для Chrome и Firefox)

```bash
bash ops/fetch-vendor.sh
```

Без этого шага сервис работает, но в Chrome и Firefox не будет запасного
HLS-варианта, если в сети зрителя закрыт UDP.

## 3. Первый запуск

```bash
docker compose up -d --build
docker compose run --rm api alembic upgrade head
docker compose exec -it api python -m app.cli create-admin --email admin@company.ru
```

Проверка:

```bash
docker compose ps
```

```bash
curl -s https://cam.company.ru/healthz
```

Все контейнеры должны быть `healthy`, `/healthz` — отвечать `ok`.

## 4. Первая камера

Откройте `https://<домен>/`, войдите, нажмите «Добавить камеру» и вставьте
RTSP-ссылку:

```
rtsp://логин:пароль@31.148.246.249:4259/stream
```

Спецсимволы в логине и пароле записывайте в процентной кодировке:
`@` → `%40`, `:` → `%3A`, `#` → `%23`, `/` → `%2F`.

В течение минуты в карточке камеры появится результат пробы кодеков. Если
там написано, что нужен транскод, — переключите профиль в настройках камеры.

Дальше: «Создать ссылку» на карточке камеры. Ссылка показывается **один раз** —
в базе хранится только её хеш.

## 5. Второй фактор

По умолчанию (`TOTP_POLICY=admins`) администраторов после входа сразу ведёт
на настройку 2FA. Чтобы требовать её со всех, поставьте `TOTP_POLICY=all`
и перезапустите `api`.

## 6. Регулярные задачи

```bash
sudo crontab -e
```

```
0 3 * * * /opt/rtspgw/ops/backup.sh >> /var/log/rtspgw-backup.log 2>&1
```

Мониторинг (необязательно):

```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

Grafana слушает только `127.0.0.1:3000` — открывайте её через SSH-туннель.

## Обновление

```bash
git pull && docker compose up -d --build && docker compose run --rm api alembic upgrade head
```

При обновлении образа MediaMTX сверьте набор ключей в `mediamtx/mediamtx.yml`
с `mediamtx.yml` соответствующего тега: на незнакомом ключе MediaMTX не
стартует. Пути при этом не потеряются — реконсилятор восстановит их из базы.
