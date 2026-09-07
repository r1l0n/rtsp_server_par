#!/usr/bin/env bash
# Скачивает hls.js — единственную стороннюю JS-библиотеку проекта.
#
# Она нужна только как фолбэк для Chrome и Firefox, когда в сети зрителя
# закрыт UDP и WebRTC не проходит. Safari и iOS играют HLS нативно.
# CSP страницы разрешает скрипты только со своего домена, поэтому CDN
# подключить нельзя — файл должен лежать локально.
#
# ЦЕЛОСТНОСТЬ. Эталонная сумма лежит в репозитории рядом с этим скриптом
# (backend/app/web/static/vendor/hls.min.js.sha256) и сверяется на каждом
# запуске. Скрипт её НЕ создаёт: раньше он записывал туда то, что отдал CDN
# при первом скачивании, и на свежей машине эталоном становился ровно тот
# файл, который требовалось проверить. Сверка появлялась только со второго
# запуска — то есть тогда, когда она уже почти не нужна.
#
# Файл попадает в образ и исполняется в браузере зрителя на странице, где
# лежит cookie доступа к медиа. CSP от подменённого локального файла не
# защищает — он и есть 'self'.
#
# Как перевыпустить эталон при смене версии — см. ниже по тексту ошибки
# и docs/security.md.
set -euo pipefail

HLS_VERSION="${HLS_VERSION:-1.5.20}"
VENDOR_DIR="$(cd "$(dirname "$0")/.." && pwd)/backend/app/web/static/vendor"
TARGET="$VENDOR_DIR/hls.min.js"
SUMFILE="$VENDOR_DIR/hls.min.js.sha256"
URL="https://cdn.jsdelivr.net/npm/hls.js@${HLS_VERSION}/dist/hls.min.js"

repin_hint() {
    cat >&2 <<HINT

Эталон обновляется только через реестр npm, а не через CDN — иначе проверка
сверяет источник сам с собой. Порядок на машине с доверенным npm:

    npm pack hls.js@${HLS_VERSION}              # npm сам сверит integrity тарбола
    tar -xzO -f hls.js-${HLS_VERSION}.tgz package/dist/hls.min.js | sha256sum

Полученную сумму записать в
    ${SUMFILE}
в формате «<sha256>  <версия>» и закоммитить вместе с правкой HLS_VERSION.
HINT
}

if [ ! -f "$SUMFILE" ]; then
    echo "ОШИБКА: нет эталонной контрольной суммы: $SUMFILE" >&2
    echo "Скачивать библиотеку, не с чем сверить, скрипт не будет." >&2
    repin_hint
    exit 1
fi

expected="$(awk 'NR==1 {print $1}' "$SUMFILE")"
pinned="$(awk 'NR==1 {print $2}' "$SUMFILE")"

if [ -z "$expected" ] || [ -z "$pinned" ]; then
    echo "ОШИБКА: $SUMFILE повреждён — ожидается строка «<sha256>  <версия>»." >&2
    repin_hint
    exit 1
fi

# Версия в эталоне и запрошенная обязаны совпадать. Без этой проверки смена
# HLS_VERSION выглядела бы как сработавшая защита от подмены: сумма не сходится,
# а причина — другая версия, а не подделка.
if [ "$pinned" != "$HLS_VERSION" ]; then
    echo "ОШИБКА: эталон записан для версии $pinned, а запрошена $HLS_VERSION." >&2
    repin_hint
    exit 1
fi

echo "Скачиваю hls.js ${HLS_VERSION}..."
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
     --max-time 60 --output "$tmp" "$URL"

actual="$(sha256sum "$tmp" | cut -d' ' -f1)"

if [ "$actual" != "$expected" ]; then
    echo "ОШИБКА: контрольная сумма не совпала." >&2
    echo "  ожидалась: $expected" >&2
    echo "  получена:  $actual" >&2
    echo "Файл НЕ установлен. Это либо подмена на CDN, либо устаревший эталон." >&2
    repin_hint
    exit 1
fi

echo "Контрольная сумма совпала с эталоном из репозитория."

mv "$tmp" "$TARGET"
trap - EXIT
echo "Готово: $TARGET ($(du -h "$TARGET" | cut -f1))"
echo "Перезапустите api, чтобы файл попал в образ: docker compose up -d --build api"
