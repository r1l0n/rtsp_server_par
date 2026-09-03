#!/usr/bin/env bash
#
# Интерактивная установка RTSP Gateway на чистый Linux-сервер.
#
#     sudo bash install.sh
#
# Скрипт спрашивает всё необходимое, проверяет введённое, ставит Docker,
# генерирует секреты, поднимает сервисы, накатывает миграции и создаёт
# администратора. Повторный запуск безопасен: существующий ключ шифрования
# не трогается никогда.
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Оформление
# ─────────────────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
    RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'
else
    B=""; DIM=""; R=""; RED=""; GRN=""; YLW=""; CYN=""
fi

STEP_NO=0
step()  { STEP_NO=$((STEP_NO + 1)); printf '\n%s[%d/%d] %s%s\n' "$B$CYN" "$STEP_NO" "$TOTAL_STEPS" "$1" "$R"; }
ok()    { printf '  %s✓%s %s\n' "$GRN" "$R" "$1"; }
info()  { printf '  %s·%s %s\n' "$DIM" "$R" "$1"; }
warn()  { printf '  %s!%s %s\n' "$YLW" "$R" "$1"; }
die()   { printf '\n%sОШИБКА:%s %s\n\n' "$RED$B" "$R" "$1" >&2; exit 1; }

TOTAL_STEPS=10

#: Тестовый CA Let's Encrypt: сертификаты недоверенные, зато без лимита
#: «5 штук на набор имён в неделю» — нужен при повторных прогонах установки.
ACME_CA_STAGING="https://acme-staging-v02.api.letsencrypt.org/directory"

#: UID пользователя внутри контейнера приложения. Должен совпадать с useradd
#: в backend/Dockerfile: под ним читается смонтированный файл ключа.
APP_UID=10001

# Скрипт идемпотентен: .env можно переиспользовать, ключ шифрования никогда
# не перезаписывается, docker compose up повторяем сколько угодно.
trap 'die "сбой на строке $LINENO. Устраните причину и запустите скрипт заново — уже сделанное он не сломает."' ERR

banner() {
    printf '%s\n' "$B$CYN"
    cat <<'ART'
  ┌─────────────────────────────────────────────┐
  │   RTSP Gateway — установка                  │
  │   камеры RTSP  ─▶  публичные https-ссылки   │
  └─────────────────────────────────────────────┘
ART
    printf '%s' "$R"
}

# ─────────────────────────────────────────────────────────────────────────────
# Ввод с проверкой
# ─────────────────────────────────────────────────────────────────────────────

# ask <переменная> <вопрос> <значение-по-умолчанию> [регулярка] [подсказка-при-ошибке]
ask() {
    local __var="$1" prompt="$2" default="${3:-}" pattern="${4:-}" hint="${5:-Неверный формат}"
    local value
    while true; do
        if [ -n "$default" ]; then
            read -r -p "  ${prompt} ${DIM}[${default}]${R}: " value || die "ввод прерван"
            value="${value:-$default}"
        else
            read -r -p "  ${prompt}: " value || die "ввод прерван"
        fi
        if [ -z "$value" ]; then
            warn "Значение обязательно."
            continue
        fi
        if [ -n "$pattern" ] && ! [[ "$value" =~ $pattern ]]; then
            warn "$hint"
            continue
        fi
        printf -v "$__var" '%s' "$value"
        return 0
    done
}

# ask_secret <переменная> <вопрос> <минимальная длина>
ask_secret() {
    local __var="$1" prompt="$2" minlen="${3:-12}"
    local first second
    while true; do
        read -r -s -p "  ${prompt}: " first || die "ввод прерван"; echo
        if [ "${#first}" -lt "$minlen" ]; then
            warn "Не короче $minlen символов."
            continue
        fi
        read -r -s -p "  Повторите: " second || die "ввод прерван"; echo
        if [ "$first" != "$second" ]; then
            warn "Не совпадает, ещё раз."
            continue
        fi
        printf -v "$__var" '%s' "$first"
        return 0
    done
}

# confirm <вопрос> <да|нет по умолчанию>
confirm() {
    local prompt="$1" default="${2:-да}" reply hint
    [ "$default" = "да" ] && hint="Д/н" || hint="д/Н"
    while true; do
        read -r -p "  ${prompt} ${DIM}[${hint}]${R}: " reply || die "ввод прерван"
        reply="${reply:-$default}"
        # Регистр кириллицы приводим руками: ${var,,} под локалью C
        # не-ASCII символы не трогает.
        case "$reply" in
            д|Д|да|Да|ДА|y|Y|yes|YES) return 0 ;;
            н|Н|нет|Нет|НЕТ|n|N|no|NO) return 1 ;;
            *) warn "Ответьте «д» или «н»." ;;
        esac
    done
}

# choose <переменная> <вопрос> <вариант:описание> ...
choose() {
    local __var="$1" prompt="$2"; shift 2
    local options=("$@") i reply
    echo "  $prompt"
    for i in "${!options[@]}"; do
        printf '    %s%d)%s %s\n' "$B" "$((i + 1))" "$R" "${options[$i]#*:}"
    done
    while true; do
        read -r -p "  Выбор ${DIM}[1]${R}: " reply || die "ввод прерван"
        reply="${reply:-1}"
        if [[ "$reply" =~ ^[0-9]+$ ]] && [ "$reply" -ge 1 ] && [ "$reply" -le "${#options[@]}" ]; then
            printf -v "$__var" '%s' "${options[$((reply - 1))]%%:*}"
            return 0
        fi
        warn "Введите число от 1 до ${#options[@]}."
    done
}

random_alnum() {
    # Только буквы и цифры: пароль попадает в DATABASE_URL, и спецсимволы
    # там пришлось бы кодировать процентами.
    #
    # Источник — openssl, а не `tr < /dev/urandom | head`: во втором случае
    # head закрывает канал, tr получает SIGPIPE, и при set -o pipefail весь
    # конвейер считается упавшим.
    local n="${1:-32}"
    openssl rand -base64 $((n * 3)) | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c "1-${n}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 1. Проверка окружения
# ─────────────────────────────────────────────────────────────────────────────
check_environment() {
    step "Проверяю окружение"

    [ -t 0 ] || die "скрипту нужен интерактивный терминал. Запускайте так: sudo bash install.sh (не через pipe)."
    [ "$(id -u)" -eq 0 ] || die "нужны права root. Запустите: sudo bash install.sh"
    [ -f docker-compose.yml ] || die "запускайте скрипт из корня проекта (рядом с docker-compose.yml)."

    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DISTRO_ID="${ID:-unknown}"
        DISTRO_LIKE="${ID_LIKE:-}"
        ok "Система: ${PRETTY_NAME:-$DISTRO_ID}"
    else
        DISTRO_ID="unknown"; DISTRO_LIKE=""
        warn "Не удалось определить дистрибутив."
    fi

    local missing=()
    for tool in openssl curl awk sed grep; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        info "Доставляю базовые утилиты: ${missing[*]}"
        if command -v apt-get >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get update -qq
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl curl gawk sed grep >/dev/null
        elif command -v dnf >/dev/null 2>&1; then
            dnf -y -q install openssl curl gawk sed grep
        else
            die "не хватает утилит: ${missing[*]}. Установите их вручную."
        fi
        ok "Утилиты установлены"
    fi

    # Значения по умолчанию на случай нестандартного вывода: пустая строка
    # в арифметическом сравнении ниже уронила бы скрипт.
    local mem_mb cpus disk_gb
    mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || true)
    cpus=$(nproc 2>/dev/null || true)
    disk_gb=$(df -BG --output=avail . 2>/dev/null | tail -1 | tr -dc '0-9' || true)
    mem_mb=${mem_mb:-0}; cpus=${cpus:-1}; disk_gb=${disk_gb:-0}

    info "Ресурсы: ${cpus} vCPU, ${mem_mb} МБ RAM, ${disk_gb} ГБ свободно"
    [ "$mem_mb" -ge 1800 ] || warn "Меньше 2 ГБ RAM — сервис поднимется, но транскодирование будет тяжёлым."
    [ "$disk_gb" -ge 10 ]  || warn "Меньше 10 ГБ свободного места."

    local busy=""
    if command -v ss >/dev/null 2>&1; then
        busy=$(ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eo ':(80|443)$' | sort -u || true)
    fi
    if [ -n "$busy" ]; then
        warn "Порты 80/443 уже кем-то заняты (nginx? apache?). Caddy не сможет их занять."
        confirm "Всё равно продолжить?" "нет" || die "освободите порты и запустите скрипт заново."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 2. Docker
# ─────────────────────────────────────────────────────────────────────────────
install_docker() {
    step "Docker"

    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ok "Docker и плагин compose уже установлены ($(docker --version | cut -d, -f1))"
        DC="docker compose"
        systemctl is-active --quiet docker || systemctl enable --now docker
        return 0
    fi

    warn "Docker или плагин compose не найдены."
    echo "  Будет добавлен официальный репозиторий Docker и установлены пакеты:"
    echo "    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
    confirm "Установить?" "да" || die "Docker обязателен. Установите его вручную и запустите скрипт снова."

    case "${DISTRO_ID}${DISTRO_LIKE}" in
        *debian*|*ubuntu*)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y -qq ca-certificates curl gnupg >/dev/null
            install -m 0755 -d /etc/apt/keyrings
            # Производные вроде Linux Mint или Raspbian берут репозиторий Debian.
            local repo="$DISTRO_ID"
            if [[ "$DISTRO_ID" != "debian" && "$DISTRO_ID" != "ubuntu" ]]; then
                [[ "$DISTRO_LIKE" == *ubuntu* ]] && repo="ubuntu" || repo="debian"
            fi
            curl -fsSL "https://download.docker.com/linux/${repo}/gpg" \
                | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
            chmod a+r /etc/apt/keyrings/docker.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${repo} ${VERSION_CODENAME:-${UBUNTU_CODENAME:-stable}} stable" \
                > /etc/apt/sources.list.d/docker.list
            apt-get update -qq
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
                docker-buildx-plugin docker-compose-plugin >/dev/null
            ;;
        *fedora*|*rhel*|*centos*|*rocky*|*almalinux*)
            dnf -y -q install dnf-plugins-core
            dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
            dnf -y -q install docker-ce docker-ce-cli containerd.io \
                docker-buildx-plugin docker-compose-plugin
            ;;
        *)
            die "неизвестный дистрибутив «$DISTRO_ID». Установите Docker вручную: https://docs.docker.com/engine/install/"
            ;;
    esac

    systemctl enable --now docker
    docker compose version >/dev/null 2>&1 || die "плагин docker compose не заработал."
    DC="docker compose"
    ok "Docker установлен: $(docker --version | cut -d, -f1)"
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 3. Опрос
# ─────────────────────────────────────────────────────────────────────────────
detect_public_ip() {
    local ip=""
    ip=$(ip -4 route get 1.1.1.1 2>/dev/null \
         | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)
    printf '%s' "$ip"
}

is_ipv4() {
    [[ "$1" =~ ^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$ ]]
}

# Приватный или иначе не маршрутизируемый в интернете адрес. Для таких
# Let's Encrypt не сможет пройти проверку, и остаётся только свой CA.
is_private_ipv4() {
    local a b IFS=.
    read -r a b _ _ <<< "$1"
    case "$a" in
        10|127|0) return 0 ;;
        169) [ "$b" = "254" ] && return 0 ;;
        172) [ "$b" -ge 16 ] && [ "$b" -le 31 ] && return 0 ;;
        192) [ "$b" = "168" ] && return 0 ;;
        100) [ "$b" -ge 64 ] && [ "$b" -le 127 ] && return 0 ;;
    esac
    return 1
}

# 203.0.113.10 -> 203-0-113-10.sslip.io
#
# sslip.io — публичный DNS-сервис, который резолвит такие имена обратно в
# зашитый в них адрес. Домен покупать не нужно, а Let's Encrypt проверяет
# его как обычное имя и выдаёт нормальный сертификат.
sslip_name() {
    printf '%s.sslip.io' "${1//./-}"
}

# Спрашивает адрес сервиса и способ получения сертификата.
# Выставляет DOMAIN, TLS_ISSUER, PUBLIC_HOST и ACCESS_MODE.
ask_address() {
    local server_ip resolved
    server_ip="$(detect_public_ip)"

    echo
    choose ACCESS_MODE "Как будете открывать панель и раздавать ссылки?" \
        "domain:Есть домен — сертификат Let's Encrypt, ничего не предупреждает (лучший вариант)" \
        "sslip:Домена нет, IP публичный — адрес вида 203-0-113-10.sslip.io, сертификат тоже настоящий" \
        "selfsigned:Только IP — свой сертификат, браузер будет предупреждать (для внутренней сети)"

    case "$ACCESS_MODE" in
        domain)   ask_address_domain "$server_ip" ;;
        sslip)    ask_address_sslip "$server_ip" ;;
        selfsigned) ask_address_selfsigned "$server_ip" ;;
    esac
}

ask_address_domain() {
    local server_ip="$1" resolved
    echo
    echo "  ${DIM}A-запись домена должна уже указывать на этот сервер — иначе${R}"
    echo "  ${DIM}Let's Encrypt не сможет проверить владение и не выдаст сертификат.${R}"
    ask DOMAIN "Домен" "" \
        '^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$' \
        "Похоже на опечатку. Пример: cam.company.ru"

    resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
    if [ -z "$resolved" ]; then
        warn "Домен $DOMAIN пока никуда не резолвится."
        warn "Сертификат не выпустится, пока не появится A-запись."
        confirm "Продолжить всё равно?" "да" || die "настройте DNS и запустите скрипт заново."
    elif [ -n "$server_ip" ] && [ "$resolved" != "$server_ip" ]; then
        warn "DNS: $DOMAIN → $resolved, а адрес этого сервера — $server_ip."
        warn "Если сервер за NAT или балансировщиком, это нормально."
        confirm "Продолжить?" "да" || die "поправьте DNS и запустите скрипт заново."
    else
        ok "DNS в порядке: $DOMAIN → $resolved"
    fi

    ask_acme_email
    echo
    echo "  ${DIM}Адрес, который уходит браузеру в WebRTC-кандидатах. Обычно${R}"
    echo "  ${DIM}совпадает с доменом; если сервер за NAT — публичный IP.${R}"
    ask PUBLIC_HOST "Публичный адрес для WebRTC" "$DOMAIN"
}

ask_address_sslip() {
    local server_ip="$1" ip resolved
    echo
    echo "  ${DIM}Из IP получится имя вида 203-0-113-10.sslip.io. Публичный сервис${R}"
    echo "  ${DIM}sslip.io резолвит его обратно в этот же адрес, поэтому Let's Encrypt${R}"
    echo "  ${DIM}выдаёт на него обычный сертификат. Домен покупать не нужно.${R}"
    while true; do
        ask ip "Публичный IP этого сервера" "$server_ip"
        if ! is_ipv4 "$ip"; then
            warn "Это не похоже на IPv4-адрес."
            continue
        fi
        if is_private_ipv4 "$ip"; then
            warn "$ip — приватный адрес. Let's Encrypt не сможет до него достучаться."
            warn "Для внутренней сети выберите третий вариант — свой сертификат."
            confirm "Всё равно использовать этот адрес?" "нет" && break
            continue
        fi
        break
    done

    DOMAIN="$(sslip_name "$ip")"
    PUBLIC_HOST="$ip"
    ok "Адрес сервиса: https://${DOMAIN}"

    resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
    if [ "$resolved" = "$ip" ]; then
        ok "sslip.io отвечает правильно: $DOMAIN → $resolved"
    else
        warn "Проверить $DOMAIN через DNS не удалось (получено: ${resolved:-ничего})."
        warn "Возможно, DNS сервера блокирует сторонние зоны. Тогда сертификат"
        warn "не выпустится — используйте свой домен или вариант со своим CA."
        confirm "Продолжить?" "да" || die "выберите другой режим и запустите скрипт заново."
    fi

    ask_acme_email
}

ask_address_selfsigned() {
    local server_ip="$1" ip
    echo
    echo "  ${DIM}Caddy выпустит сертификат собственным центром сертификации.${R}"
    echo "  ${DIM}Трафик шифруется полноценно, но браузер будет показывать${R}"
    echo "  ${DIM}предупреждение, пока корневой сертификат не установлен на${R}"
    echo "  ${DIM}машины зрителей. В конце установки будет команда, как его забрать.${R}"
    while true; do
        ask ip "IP-адрес этого сервера" "$server_ip"
        is_ipv4 "$ip" && break
        warn "Это не похоже на IPv4-адрес."
    done

    DOMAIN="$ip"
    PUBLIC_HOST="$ip"
    TLS_ISSUER="internal"
    ok "Адрес сервиса: https://${ip} (сертификат свой)"
}

ask_acme_email() {
    echo
    echo "  ${DIM}Почта для Let's Encrypt: туда придёт письмо, если сертификат${R}"
    echo "  ${DIM}вдруг перестанет обновляться.${R}"
    ask ACME_EMAIL "Почта администратора" "" \
        '^[^@[:space:]]+@[^@[:space:]]+\.[a-zA-Z]{2,}$' "Введите корректный адрес."
    TLS_ISSUER="$ACME_EMAIL"

    echo
    echo "  ${DIM}У Let's Encrypt лимит: 5 сертификатов на один набор имён${R}"
    echo "  ${DIM}в неделю. Если вы ставите сервис несколько раз подряд, чтобы${R}"
    echo "  ${DIM}попробовать разные режимы, в него легко упереться и потерять${R}"
    echo "  ${DIM}неделю. Тестовый CA лимитов не имеет, но браузер будет${R}"
    echo "  ${DIM}предупреждать — как со своим сертификатом.${R}"
    if confirm "Это пробная установка, использовать тестовый CA?" "нет"; then
        ACME_CA="$ACME_CA_STAGING"
        warn "Включён тестовый CA — сертификат будет недоверенным."
        info "Для боевого запуска уберите ACME_CA из .env и перезапустите caddy."
    fi
}

collect_settings() {
    step "Настройки"

    # Значения по умолчанию задаём до всех ветвлений: при повторном запуске
    # часть вопросов не задаётся, а переменные всё равно читаются дальше,
    # и под set -u это было бы падением.
    REUSE_ENV=0
    MONITORING=0
    FETCH_HLS=0
    SETUP_FIREWALL=0
    ACCESS_MODE=domain
    ACME_CA=""

    if [ -f .env ]; then
        warn "Файл .env уже существует."
        if confirm "Использовать его и не спрашивать настройки заново?" "да"; then
            # shellcheck disable=SC1091
            set -a; . ./.env; set +a
            REUSE_ENV=1
            if [ -n "${GRAFANA_PASSWORD:-}" ]; then
                MONITORING=1
            fi
            [ "${TLS_ISSUER:-}" = "internal" ] && ACCESS_MODE=selfsigned
            ok "Использую существующий .env (адрес: ${DOMAIN:-не задан})"
            [ -n "${DOMAIN:-}" ] || die "в .env не задан DOMAIN. Удалите файл и запустите скрипт заново."
            [ -n "${TLS_ISSUER:-}" ] || die "в .env не задан TLS_ISSUER (почта или слово internal)."
            ask_remaining
            return 0
        fi
        cp -a .env ".env.backup-$(date +%Y%m%d-%H%M%S)"
        info "Старый .env сохранён рядом с суффиксом .backup-*"
    fi

    ask_address

    echo
    choose TOTP_POLICY "Кому обязателен второй фактор (2FA)?" \
        "admins:Администраторам — остальным по желанию (рекомендуется)" \
        "all:Всем без исключения" \
        "optional:Никому не обязателен"

    echo
    echo "  ${DIM}Пароль PostgreSQL человеком не вводится — он живёт только внутри${R}"
    echo "  ${DIM}docker-сети. Генерирую случайный.${R}"
    POSTGRES_PASSWORD="$(random_alnum 32)"
    ok "Пароль базы сгенерирован"

    echo
    if confirm "Поднять мониторинг (Prometheus + Grafana)?" "нет"; then
        MONITORING=1
        echo "  ${DIM}Grafana будет доступна только на 127.0.0.1:3000 — через SSH-туннель.${R}"
        ask_secret GRAFANA_PASSWORD "Пароль администратора Grafana" 12
    fi

    ask_remaining
}

# Вопросы, которые задаются в обоих случаях — и при первой установке,
# и при повторном запуске с готовым .env.
ask_remaining() {
    echo
    echo "  ${DIM}hls.js — запасной вариант для Chrome и Firefox, когда в сети${R}"
    echo "  ${DIM}зрителя закрыт UDP и WebRTC не проходит. Будет скачан с jsdelivr.${R}"
    if [ -s backend/app/web/static/vendor/hls.min.js ]; then
        info "hls.js уже на месте — пропускаю."
    elif confirm "Скачать hls.js?" "да"; then
        FETCH_HLS=1
    fi

    if command -v ufw >/dev/null 2>&1 || command -v firewall-cmd >/dev/null 2>&1; then
        echo
        echo "  ${DIM}Наружу нужны только ssh, 80, 443 и 8189/udp.${R}"
        if confirm "Настроить файрвол?" "да"; then
            SETUP_FIREWALL=1
        fi
    fi

    echo
    ask ADMIN_EMAIL "Почта администратора панели" "${ACME_EMAIL:-}" \
        '^[^@[:space:]]+@[^@[:space:]]+\.[a-zA-Z]{2,}$' "Введите корректный адрес."
    info "Пароль администратора задаётся в конце, интерактивно."
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 4. .env
# ─────────────────────────────────────────────────────────────────────────────
write_env() {
    step "Конфигурация"

    if [ "$REUSE_ENV" = "1" ]; then
        ok "Файл .env оставлен без изменений"
        return 0
    fi

    cat > .env <<ENV
# Сгенерировано install.sh $(date -Is)

# ─── Адрес и TLS ────────────────────────────────────────────────────────────
# TLS_ISSUER: почта → сертификат Let's Encrypt; internal → собственный CA.
DOMAIN=${DOMAIN}
TLS_ISSUER=${TLS_ISSUER}
PUBLIC_HOST=${PUBLIC_HOST}

# ─── База данных ────────────────────────────────────────────────────────────
POSTGRES_USER=rtspgw
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=rtspgw
DATABASE_URL=postgresql+asyncpg://rtspgw:${POSTGRES_PASSWORD}@postgres:5432/rtspgw

# ─── Redis ──────────────────────────────────────────────────────────────────
REDIS_URL=redis://redis:6379/0

# ─── MediaMTX ───────────────────────────────────────────────────────────────
MTX_API_URL=http://mediamtx:9997

# ─── Приложение ─────────────────────────────────────────────────────────────
APP_SECRET_KEY_FILE=/run/secrets/app_key
LOG_LEVEL=INFO
SESSION_COOKIE_SECURE=true
SESSION_TTL_MINUTES=720
DEFAULT_LINK_TTL_HOURS=24
TOTP_POLICY=${TOTP_POLICY}
ALLOW_PRIVATE_CAMERA_HOSTS=false
CAMERA_HOST_ALLOWLIST=
ENV

    if [ -n "${ACME_CA:-}" ]; then
        printf '\n# Тестовый CA: сертификат недоверенный, зато без недельных лимитов.\nACME_CA=%s\n' \
            "$ACME_CA" >> .env
    fi

    if [ "$MONITORING" = "1" ]; then
        printf '\n# ─── Мониторинг ─────────────────────────────────────────────────────────────\nGRAFANA_PASSWORD=%s\n' \
            "$GRAFANA_PASSWORD" >> .env
    fi

    chmod 600 .env
    ok "Записан .env (права 600)"
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 5. Ключ шифрования
# ─────────────────────────────────────────────────────────────────────────────
# Права на файл ключа.
#
# Docker монтирует secrets/app_key внутрь контейнера как есть — с владельцем
# и правами, которые файл имеет на хосте. Процесс в контейнере работает под
# непривилегированным uid, поэтому файл, принадлежащий root с правами 600,
# ему не прочитать. Отдаём файл этому uid и оставляем 400: на хосте ключ
# по-прежнему доступен только root (и владельцу), в контейнере — только чтение.
secure_key_file() {
    chown "$APP_UID:$APP_UID" secrets/app_key
    chmod 400 secrets/app_key
}

generate_key() {
    step "Ключ шифрования"

    mkdir -p secrets
    chmod 700 secrets

    if [ -s secrets/app_key ]; then
        ok "Ключ уже существует — не трогаю его"
        info "Перезапись ключа сделала бы учётные данные всех камер нечитаемыми."
        secure_key_file
        return 0
    fi

    openssl rand -base64 32 > secrets/app_key
    secure_key_file
    ok "Ключ сгенерирован: secrets/app_key"

    echo
    printf '  %s╔════════════════════════════════════════════════════════════════╗%s\n' "$YLW$B" "$R"
    printf '  %s║  СОХРАНИТЕ ЭТОТ КЛЮЧ В МЕНЕДЖЕР ПАРОЛЕЙ КОМПАНИИ               ║%s\n' "$YLW$B" "$R"
    printf '  %s╚════════════════════════════════════════════════════════════════╝%s\n' "$YLW$B" "$R"
    echo
    printf '      %s%s%s\n' "$B" "$(cat secrets/app_key)" "$R"
    echo
    echo "  Им зашифрованы учётные данные камер и TOTP-секреты."
    echo "  Резервная копия базы без этого ключа бесполезна."
    echo
    read -r -p "  Нажмите Enter, когда сохраните ключ… " _ || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 6. Файрвол
# ─────────────────────────────────────────────────────────────────────────────
setup_firewall() {
    step "Файрвол"

    if [ "$SETUP_FIREWALL" != "1" ]; then
        info "Пропущено по вашему выбору."
        warn "Не забудьте открыть 80/tcp, 443/tcp и 8189/udp."
        return 0
    fi

    # Порт SSH определяем заранее: включить файрвол, не разрешив свой же
    # ssh — это отрезать себе доступ к серверу.
    local ssh_port=22
    if [ -n "${SSH_CONNECTION:-}" ]; then
        ssh_port="$(awk '{print $4}' <<< "$SSH_CONNECTION")"
    elif [ -r /etc/ssh/sshd_config ]; then
        ssh_port="$(awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/ {print $2; exit}' /etc/ssh/sshd_config)"
        ssh_port="${ssh_port:-22}"
    fi
    info "Порт SSH определён как $ssh_port — он будет разрешён первым."

    if command -v ufw >/dev/null 2>&1; then
        ufw allow "${ssh_port}/tcp"  >/dev/null
        ufw allow 80/tcp             >/dev/null
        ufw allow 443/tcp            >/dev/null
        ufw allow 8189/udp           >/dev/null
        if ! ufw status | grep -q '^Status: active'; then
            if confirm "Включить ufw прямо сейчас?" "да"; then
                ufw --force enable >/dev/null
            fi
        fi
        ok "ufw: разрешены ${ssh_port}/tcp, 80/tcp, 443/tcp, 8189/udp"
    elif command -v firewall-cmd >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port="${ssh_port}/tcp" >/dev/null
        firewall-cmd --permanent --add-port=80/tcp            >/dev/null
        firewall-cmd --permanent --add-port=443/tcp           >/dev/null
        firewall-cmd --permanent --add-port=8189/udp          >/dev/null
        firewall-cmd --reload >/dev/null
        ok "firewalld: разрешены ${ssh_port}/tcp, 80/tcp, 443/tcp, 8189/udp"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 7. hls.js
# ─────────────────────────────────────────────────────────────────────────────
fetch_vendor() {
    step "Библиотека плеера"

    if [ "$FETCH_HLS" != "1" ]; then
        info "Пропущено. Сервис будет работать, но без HLS-фолбэка в Chrome и Firefox."
        info "Скачать позже: bash ops/fetch-vendor.sh"
        return 0
    fi

    local log="/tmp/rtspgw-vendor.log"
    if bash ops/fetch-vendor.sh > "$log" 2>&1; then
        ok "hls.js установлен"
    else
        tail -3 "$log" | sed 's/^/      /'
        warn "Не удалось скачать hls.js — продолжаю без него."
        info "Повторить позже: bash ops/fetch-vendor.sh"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 8. Сборка и запуск
# ─────────────────────────────────────────────────────────────────────────────
compose_files() {
    if [ "${MONITORING:-0}" = "1" ]; then
        printf '%s' "-f docker-compose.yml -f docker-compose.monitoring.yml"
    else
        printf '%s' "-f docker-compose.yml"
    fi
}

wait_healthy() {
    local deadline=$((SECONDS + 180)) spin='|/-\' i=0
    while [ "$SECONDS" -lt "$deadline" ]; do
        local bad=0 total=0 cid state
        for cid in $($DC $(compose_files) ps -q 2>/dev/null); do
            total=$((total + 1))
            state=$(docker inspect -f \
                '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                "$cid" 2>/dev/null || echo unknown)
            case "$state" in
                healthy|running) ;;
                *) bad=$((bad + 1)) ;;
            esac
        done
        if [ "$total" -gt 0 ] && [ "$bad" -eq 0 ]; then
            printf '\r'; return 0
        fi
        i=$(((i + 1) % 4))
        printf '\r  %s%s%s жду готовности сервисов (%d из %d)…  ' \
            "$DIM" "${spin:$i:1}" "$R" "$((total - bad))" "$total"
        sleep 2
    done
    printf '\r'
    return 1
}

# Показывает логи контейнеров, которые не поднялись.
#
# Compose про упавший контейнер сообщает только «is unhealthy» — по этой
# строке причину не найти, а лезть за логами руками в момент установки
# неудобно. Поэтому показываем их сразу.
show_container_failures() {
    local cid name state shown=0
    for cid in $($DC $(compose_files) ps -aq 2>/dev/null); do
        state=$(docker inspect -f \
            '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$cid" 2>/dev/null || echo unknown)
        case "$state" in
            healthy|running) continue ;;
        esac
        name=$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's|^/||' || echo "$cid")
        shown=$((shown + 1))
        echo
        printf '  %s%s — %s%s\n' "$RED$B" "$name" "$state" "$R"
        docker logs --tail 25 "$cid" 2>&1 | sed 's/^/      /'
    done
    [ "$shown" -eq 0 ] && info "Все контейнеры выглядят живыми — смотрите логи целиком."
    echo
    echo "  Полные логи: ${B}docker compose logs --tail=100${R}"
}

build_and_start() {
    step "Сборка и запуск"

    info "Собираю образ приложения — первый раз это пара минут…"
    $DC $(compose_files) build --quiet
    ok "Образ собран"

    if ! $DC $(compose_files) up -d; then
        warn "Контейнеры не запустились."
        show_container_failures
        die "устраните причину и запустите скрипт заново — уже сделанное он не сломает."
    fi
    ok "Контейнеры запущены"

    if wait_healthy; then
        ok "Все сервисы здоровы"
    else
        warn "Не все сервисы вышли в healthy за 3 минуты."
        show_container_failures
        confirm "Продолжить установку?" "нет" || die "разберитесь с логами и запустите скрипт заново."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 9. Миграции
# ─────────────────────────────────────────────────────────────────────────────
run_migrations() {
    step "Схема базы данных"

    local log
    log="$(mktemp)"
    if $DC $(compose_files) run --rm api alembic upgrade head >"$log" 2>&1; then
        rm -f "$log"
        ok "Миграции применены"
    else
        warn "Миграции не применились."
        tail -30 "$log" | sed 's/^/      /'
        rm -f "$log"
        die "разберитесь с ошибкой выше и запустите скрипт заново."
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаг 10. Администратор
# ─────────────────────────────────────────────────────────────────────────────
create_admin() {
    step "Учётная запись администратора"

    echo "  Сейчас нужно задать пароль для ${B}${ADMIN_EMAIL}${R}."
    echo "  ${DIM}Не короче 12 символов. Ввод не отображается.${R}"
    echo "  ${DIM}Пароль вводится напрямую в приложение и нигде не сохраняется${R}"
    echo "  ${DIM}в истории команд.${R}"
    echo

    if $DC $(compose_files) exec -it api python -m app.cli create-admin --email "$ADMIN_EMAIL"; then
        ok "Администратор создан"
    else
        warn "Не удалось создать администратора (возможно, он уже существует)."
        echo "  Создать вручную:"
        echo "    ${B}docker compose exec -it api python -m app.cli create-admin --email ВАША@ПОЧТА${R}"
        echo "  Сбросить пароль существующему:"
        echo "    ${B}docker compose exec -it api python -m app.cli reset-password --email ВАША@ПОЧТА${R}"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Итог
# ─────────────────────────────────────────────────────────────────────────────
final_check() {
    local url="https://${DOMAIN}/healthz" body="" tries=0 insecure=()
    # В режиме своего CA curl не знает нашего корневого сертификата — здесь
    # проверяется доступность сервиса, а не доверие к цепочке.
    [ "${TLS_ISSUER:-}" = "internal" ] && insecure=(--insecure)
    printf '\n  %sПроверяю, что панель отвечает…%s ' "$DIM" "$R"
    while [ "$tries" -lt 12 ]; do
        body="$(curl -fsS "${insecure[@]}" --max-time 5 "$url" 2>/dev/null || true)"
        if [ "$body" = "ok" ]; then
            break
        fi
        tries=$((tries + 1))
        sleep 5
    done
    if [ "$body" = "ok" ]; then
        printf '%s✓%s\n' "$GRN" "$R"
        return 0
    fi
    printf '%s—%s\n' "$YLW" "$R"
    return 1
}

summary() {
    local healthy=0
    final_check && healthy=1

    printf '\n%s' "$B$GRN"
    cat <<'ART'
  ┌─────────────────────────────────────────────┐
  │              Готово                         │
  └─────────────────────────────────────────────┘
ART
    printf '%s\n' "$R"

    echo "  Панель:  ${B}https://${DOMAIN}/${R}"
    echo "  Вход:    ${B}${ADMIN_EMAIL}${R}"
    if [ "$MONITORING" = "1" ]; then
        echo "  Grafana: ${B}http://127.0.0.1:3000${R} ${DIM}(только через SSH-туннель:${R}"
        echo "           ${DIM}ssh -L 3000:localhost:3000 root@${DOMAIN})${R}"
    fi

    if [ "$healthy" != "1" ]; then
        echo
        warn "Панель пока не ответила по https."
        echo "  Обычно это значит, что Let's Encrypt ещё выпускает сертификат"
        echo "  (до минуты) или DNS не успел разойтись. Проверьте:"
        echo "    ${B}docker compose logs -f caddy${R}"
    fi

    if [ "${TLS_ISSUER:-}" = "internal" ]; then
        echo
        printf '  %sСертификат выпущен собственным CA.%s\n' "$B$YLW" "$R"
        echo "  Браузер будет предупреждать при каждом заходе, пока корневой"
        echo "  сертификат не установлен. Забрать его:"
        echo
        echo "    ${B}docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./rtspgw-ca.crt${R}"
        echo
        echo "  Дальше файл ${B}rtspgw-ca.crt${R} нужно установить в доверенные"
        echo "  корневые центры на машинах тех, кто будет смотреть камеры."
        echo "  ${DIM}Если ссылки нужно раздавать людям вне компании — лучше${R}"
        echo "  ${DIM}перевыпустить на домен или на sslip.io: тогда предупреждений${R}"
        echo "  ${DIM}не будет вовсе. Достаточно поправить DOMAIN и TLS_ISSUER в .env.${R}"
    fi

    echo
    echo "  ${B}Что дальше:${R}"
    echo "    1. Войдите в панель и настройте второй фактор."
    echo "    2. Добавьте камеру — RTSP-ссылку вида"
    echo "       ${DIM}rtsp://логин:пароль@31.148.246.249:4259/stream${R}"
    echo "       ${DIM}Спецсимволы в пароле: @ → %40, : → %3A, # → %23${R}"
    echo "    3. Через минуту в карточке камеры появится проба кодеков."
    echo "       Если написано, что нужен транскод — переключите профиль."
    echo "    4. Создайте публичную ссылку. Она показывается ${B}один раз${R}."

    echo
    echo "  ${B}Не забудьте:${R}"
    echo "    · Ключ ${B}secrets/app_key${R} — в менеджер паролей компании."
    echo "    · Бэкапы в cron:"
    echo "        ${DIM}0 3 * * * ${SCRIPT_DIR}/ops/backup.sh >> /var/log/rtspgw-backup.log 2>&1${R}"
    echo "    · Чеклист перед выпуском в прод: ${B}docs/security.md${R}"

    echo
    echo "  ${B}Полезные команды:${R}"
    echo "    docker compose ps                 ${DIM}состояние сервисов${R}"
    echo "    docker compose logs -f api        ${DIM}логи панели${R}"
    echo "    docker compose restart api        ${DIM}перезапуск${R}"
    echo "    docs/runbook.md                   ${DIM}разбор типовых сбоев${R}"
    echo
}

# ─────────────────────────────────────────────────────────────────────────────
main() {
    banner
    check_environment
    install_docker
    collect_settings
    write_env
    generate_key
    setup_firewall
    fetch_vendor
    build_and_start
    run_migrations
    create_admin
    summary
}

# Запускаемся только при прямом вызове: при `source install.sh` файл отдаёт
# свои функции, не выполняя установку. На этом держатся тесты в
# tests/test_install_sh.sh.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
