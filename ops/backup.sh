#!/usr/bin/env bash
# Резервная копия PostgreSQL и ключа шифрования.
#
# Ставится в cron:
#     0 3 * * * /opt/rtspgw/ops/backup.sh >> /var/log/rtspgw-backup.log 2>&1
#
# ВАЖНО: дамп БД без ключа шифрования бесполезен — учётные данные камер
# и TOTP-секреты зашифрованы им. Ключ копируется отдельно и ДОЛЖЕН храниться
# не там же, где дампы: иначе одна украденная папка отдаёт и то и другое.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rtspgw}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"

cd "$PROJECT_DIR"
# shellcheck disable=SC1091
set -a; . ./.env; set +a

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

dump="$BACKUP_DIR/rtspgw-$STAMP.sql.gz"
echo "[$(date -Is)] дамп БД -> $dump"
docker compose exec -T postgres \
    pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --clean --if-exists \
    | gzip -9 > "$dump"
chmod 600 "$dump"

# Проверяем, что дамп не пустой и распаковывается: молчаливо битый бэкап
# хуже отсутствующего.
if ! gzip -t "$dump" || [ "$(stat -c%s "$dump")" -lt 1024 ]; then
    echo "ОШИБКА: дамп повреждён или подозрительно мал" >&2
    exit 1
fi

echo "[$(date -Is)] удаляю копии старше $RETENTION_DAYS дней"
find "$BACKUP_DIR" -name 'rtspgw-*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

if [ -n "${OFFSITE_TARGET:-}" ]; then
    echo "[$(date -Is)] выгружаю во внешнее хранилище: $OFFSITE_TARGET"
    rsync -a --chmod=600 "$dump" "$OFFSITE_TARGET/"
fi

echo "[$(date -Is)] готово: $(du -h "$dump" | cut -f1)"
echo "Напоминание: ключ secrets/app_key должен храниться отдельно от дампов."
