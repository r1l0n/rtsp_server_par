#!/usr/bin/env bash
# Скачивает hls.js — единственную стороннюю JS-библиотеку проекта.
#
# Она нужна только как фолбэк для Chrome и Firefox, когда в сети зрителя
# закрыт UDP и WebRTC не проходит. Safari и iOS играют HLS нативно.
# CSP страницы разрешает скрипты только со своего домена, поэтому CDN
# подключить нельзя — файл должен лежать локально.
#
# Проверка целостности: при первом запуске сохраняется контрольная сумма,
# дальше она сверяется. Сверить её с официальной один раз можно так:
#     npm view hls.js@$HLS_VERSION dist.integrity
set -euo pipefail

HLS_VERSION="${HLS_VERSION:-1.5.20}"
VENDOR_DIR="$(cd "$(dirname "$0")/.." && pwd)/backend/app/web/static/vendor"
TARGET="$VENDOR_DIR/hls.min.js"
SUMFILE="$VENDOR_DIR/hls.min.js.sha256"
URL="https://cdn.jsdelivr.net/npm/hls.js@${HLS_VERSION}/dist/hls.min.js"

mkdir -p "$VENDOR_DIR"

echo "Скачиваю hls.js ${HLS_VERSION}..."
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
     --max-time 60 --output "$tmp" "$URL"

actual="$(sha256sum "$tmp" | cut -d' ' -f1)"

if [ -f "$SUMFILE" ]; then
    expected="$(cat "$SUMFILE")"
    if [ "$actual" != "$expected" ]; then
        echo "ОШИБКА: контрольная сумма не совпала." >&2
        echo "  ожидалась: $expected" >&2
        echo "  получена:  $actual" >&2
        echo "Файл не установлен. Проверьте версию и источник." >&2
        exit 1
    fi
    echo "Контрольная сумма совпала."
else
    echo "$actual" > "$SUMFILE"
    echo "Контрольная сумма сохранена: $actual"
    echo "Сверьте её один раз с официальной: npm view hls.js@${HLS_VERSION} dist.integrity"
fi

mv "$tmp" "$TARGET"
trap - EXIT
echo "Готово: $TARGET ($(du -h "$TARGET" | cut -f1))"
echo "Перезапустите api, чтобы файл попал в образ: docker compose up -d --build api"
