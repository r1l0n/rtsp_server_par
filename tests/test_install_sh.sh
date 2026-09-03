#!/usr/bin/env bash
#
# Тесты установщика. Запуск:
#
#     bash tests/test_install_sh.sh
#
# install.sh при `source` отдаёт свои функции, не выполняя установку, — это
# позволяет проверить разбор ввода и генерацию .env без сервера и без docker.
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0
SKIP=0

skip() {
    SKIP=$((SKIP + 1))
    printf '  skip %s (%s)\n' "$1" "$2"
}

# На NTFS/cygwin chmod не применяется, и проверять права там бессмысленно.
permissions_supported() {
    local probe="$1/.perm-probe"
    : > "$probe"
    chmod 600 "$probe" 2>/dev/null
    local mode
    mode="$(stat -c '%a' "$probe" 2>/dev/null || echo unknown)"
    rm -f "$probe"
    [ "$mode" = "600" ]
}

check() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        PASS=$((PASS + 1))
        printf '  ok   %s\n' "$name"
    else
        FAIL=$((FAIL + 1))
        printf '  FAIL %s\n       ожидалось: %s\n       получено:  %s\n' "$name" "$expected" "$actual"
    fi
}

check_true() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        PASS=$((PASS + 1)); printf '  ok   %s\n' "$name"
    else
        FAIL=$((FAIL + 1)); printf '  FAIL %s (ожидался успех)\n' "$name"
    fi
}

check_false() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        FAIL=$((FAIL + 1)); printf '  FAIL %s (ожидался отказ)\n' "$name"
    else
        PASS=$((PASS + 1)); printf '  ok   %s\n' "$name"
    fi
}

# Подключаем функции установщика. TERM=dumb — чтобы в выводе не было
# управляющих последовательностей и сравнения строк были честными.
TERM=dumb
# shellcheck disable=SC1091
source "$ROOT/install.sh"
set +e   # тесты сами решают, что считать ошибкой

echo
echo "── Проверка домена ─────────────────────────────────────────────────"
DOMAIN_RE='^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
matches() { [[ "$1" =~ $DOMAIN_RE ]]; }

check_true  "принимает cam.company.ru"        matches "cam.company.ru"
check_true  "принимает поддомен 3 уровня"     matches "a.b.company.co.uk"
check_true  "принимает дефисы"                matches "my-cam.company-name.ru"
check_false "отклоняет без точки"             matches "localhost"
check_false "отклоняет http-схему"            matches "https://cam.company.ru"
check_false "отклоняет пробел"                matches "cam company.ru"
check_false "отклоняет пустую строку"         matches ""
check_false "отклоняет IP-адрес"              matches "31.148.246.249"

echo
echo "── Проверка почты ──────────────────────────────────────────────────"
EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+\.[a-zA-Z]{2,}$'
is_email() { [[ "$1" =~ $EMAIL_RE ]]; }

check_true  "принимает обычный адрес"         is_email "admin@company.ru"
check_true  "принимает точку в имени"         is_email "i.ivanov@company.co.uk"
check_false "отклоняет без собаки"            is_email "admin.company.ru"
check_false "отклоняет две собаки"            is_email "a@b@company.ru"
check_false "отклоняет пробел"                is_email "admin @company.ru"
check_false "отклоняет без домена верхнего"   is_email "admin@company"

echo
echo "── Генератор паролей ───────────────────────────────────────────────"
pw="$(random_alnum 32)"
pw16="$(random_alnum 16)"
check "длина 32"           "32" "${#pw}"
check "длина 16"           "16" "${#pw16}"
check "только [A-Za-z0-9]" ""   "$(printf '%s' "$pw" | tr -d 'A-Za-z0-9')"
if [ "$(random_alnum 32)" = "$(random_alnum 32)" ]; then
    FAIL=$((FAIL + 1)); echo "  FAIL пароли повторяются"
else
    PASS=$((PASS + 1)); echo "  ok   два вызова дают разные пароли"
fi

echo
echo "── Подтверждения ───────────────────────────────────────────────────"
answer() { printf '%s\n' "$1" | confirm "вопрос" "${2:-да}" >/dev/null 2>&1; }

check_true  "«д» — это да"                    answer "д"
check_true  "«да» — это да"                   answer "да"
check_true  "«Да» с заглавной — это да"       answer "Да"
check_true  "«y» — это да"                    answer "y"
check_true  "пустой ввод берёт умолчание да"  answer "" "да"
check_false "«н» — это нет"                   answer "н"
check_false "«нет» — это нет"                 answer "нет"
check_false "«Нет» с заглавной — это нет"     answer "Нет"
check_false "«n» — это нет"                   answer "n"
check_false "пустой ввод берёт умолчание нет" answer "" "нет"

echo
echo "── Выбор из списка ─────────────────────────────────────────────────"
pick() {
    local out
    printf '%s\n' "$1" | { choose out "вопрос" "admins:А" "all:Б" "optional:В" >/dev/null 2>&1; echo "$out"; }
}
check "вариант 1"            "admins"   "$(pick 1)"
check "вариант 2"            "all"      "$(pick 2)"
check "вариант 3"            "optional" "$(pick 3)"
check "пустой ввод = первый" "admins"   "$(pick '')"

echo
echo "── Генерация .env ──────────────────────────────────────────────────"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP" || exit 1

REUSE_ENV=0
MONITORING=0
DOMAIN="cam.company.ru"
ACME_EMAIL="admin@company.ru"
PUBLIC_HOST="cam.company.ru"
POSTGRES_PASSWORD="TestPassword123456"
TOTP_POLICY="admins"
STEP_NO=0
TOTAL_STEPS=10

write_env >/dev/null 2>&1

check_true "файл .env создан" test -s .env
check "домен записан"        "DOMAIN=cam.company.ru"           "$(grep '^DOMAIN=' .env)"
check "политика 2FA"         "TOTP_POLICY=admins"              "$(grep '^TOTP_POLICY=' .env)"
check "ключ берётся из файла" "APP_SECRET_KEY_FILE=/run/secrets/app_key" "$(grep '^APP_SECRET_KEY_FILE=' .env)"
check "cookie только по https" "SESSION_COOKIE_SECURE=true"    "$(grep '^SESSION_COOKIE_SECURE=' .env)"
check "приватные хосты запрещены" "ALLOW_PRIVATE_CAMERA_HOSTS=false" "$(grep '^ALLOW_PRIVATE_CAMERA_HOSTS=' .env)"
if permissions_supported "$TMP"; then
    check "права 600" "600" "$(stat -c '%a' .env 2>/dev/null || echo '?')"
else
    skip "права 600" "файловая система не хранит права unix"
fi
check "пароль попал в DATABASE_URL" \
    "DATABASE_URL=postgresql+asyncpg://rtspgw:TestPassword123456@postgres:5432/rtspgw" \
    "$(grep '^DATABASE_URL=' .env)"
check "без мониторинга нет пароля grafana" "" "$(grep '^GRAFANA_PASSWORD=' .env || true)"

# Сгенерированный .env должен читаться шеллом как набор переменных:
# его же потом читает docker compose.
( set -a; . ./.env; set +a; [ "$DOMAIN" = "cam.company.ru" ] ) \
    && { PASS=$((PASS + 1)); echo "  ok   .env корректно читается через source"; } \
    || { FAIL=$((FAIL + 1)); echo "  FAIL .env не читается через source"; }

MONITORING=1
GRAFANA_PASSWORD="GrafanaSecret123"
rm -f .env
write_env >/dev/null 2>&1
check "с мониторингом пароль grafana есть" "GRAFANA_PASSWORD=GrafanaSecret123" \
    "$(grep '^GRAFANA_PASSWORD=' .env)"

echo
echo "── Набор compose-файлов ────────────────────────────────────────────"
MONITORING=0
check "без мониторинга — один файл" "-f docker-compose.yml" "$(compose_files)"
MONITORING=1
check "с мониторингом — два файла" \
    "-f docker-compose.yml -f docker-compose.monitoring.yml" "$(compose_files)"

echo
echo "── Повторный запуск не трогает .env ────────────────────────────────"
REUSE_ENV=1
before="$(cat .env)"
write_env >/dev/null 2>&1
check "содержимое не изменилось" "$before" "$(cat .env)"

cd "$ROOT" || exit 1

echo
if [ "$SKIP" -gt 0 ]; then
    printf 'Итого: %d успешно, %d пропущено' "$PASS" "$SKIP"
else
    printf 'Итого: %d успешно' "$PASS"
fi
if [ "$FAIL" -gt 0 ]; then
    printf ', %d ПРОВАЛЕНО\n\n' "$FAIL"
    exit 1
fi
printf '\n\n'
