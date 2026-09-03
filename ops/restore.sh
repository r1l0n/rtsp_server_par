#!/usr/bin/env bash
# Восстановление из резервной копии.
#
#     ./ops/restore.sh /var/backups/rtspgw/rtspgw-20260902-030000.sql.gz
#
# Процедуру нужно прогнать хотя бы один раз на тестовом стенде: бэкап,
# который никогда не восстанавливали, бэкапом не является.
set -euo pipefail

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "Использование: $0 <путь-к-дампу.sql.gz>" >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
# shellcheck disable=SC1091
set -a; . ./.env; set +a

cat <<WARNING
Восстановление ПЕРЕЗАПИШЕТ текущую базу $POSTGRES_DB.
Файл: $DUMP
Перед продолжением убедитесь, что secrets/app_key соответствует этому дампу:
без совпадающего ключа учётные данные камер и TOTP-секреты не расшифруются.
WARNING
read -r -p "Продолжить? (введите ДА) " answer
[ "$answer" = "ДА" ] || { echo "Отменено."; exit 1; }

echo "Останавливаю приложение (база остаётся поднятой)..."
docker compose stop api worker

echo "Заливаю дамп..."
gunzip -c "$DUMP" | docker compose exec -T postgres \
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=on

echo "Накатываю миграции (на случай, если дамп старше кода)..."
docker compose run --rm api alembic upgrade head

echo "Запускаю приложение..."
docker compose start api worker

echo "Проверка:"
docker compose ps
echo "Реконсилятор восстановит пути в MediaMTX в течение 15 секунд."
