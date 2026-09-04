#!/usr/bin/env bash
#
# Удаление RTSP. Нужно прежде всего для тестовых прогонов: снести
# всё и поставить заново другим способом, не переустанавливая систему.
#
#     sudo bash uninstall.sh              выбор уровня в диалоге
#     sudo bash uninstall.sh --status     только показать, что установлено
#     sudo bash uninstall.sh --reset -y   снести данные, конфиг оставить
#     sudo bash uninstall.sh --full -y    снести всё, кроме Docker
#     sudo bash uninstall.sh --purge      снести всё вместе с Docker
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Общие функции вывода и ввода берём из установщика: при `source` он отдаёт
# только функции и ничего не устанавливает (см. guard в конце install.sh).
[ -f install.sh ] || { echo "install.sh не найден рядом — запускайте из корня проекта" >&2; exit 1; }
# shellcheck disable=SC1091
source ./install.sh

TOTAL_STEPS=6
STEP_NO=0
trap 'die "сбой на строке $LINENO. Часть объектов могла остаться — запустите с --status и посмотрите."' ERR

PROJECT="rtspgw"
LABEL="com.docker.compose.project=$PROJECT"
IMAGE="rtspgw-api:local"

MODE=""
ASSUME_YES=0
REMOVE_BACKUPS=0
REMOVE_BASE_IMAGES=0
BACKUP_DIR_DEFAULT="/var/backups/rtspgw"

# ─────────────────────────────────────────────────────────────────────────────
usage() {
    cat <<'HELP'
Удаление RTSP.

  --status          показать, что сейчас установлено, и выйти
  --reset           контейнеры и данные (БД, Redis, сертификаты);
                    .env и ключ шифрования остаются — быстрая переустановка
  --full            + .env, ключ шифрования, hls.js, правила файрвола
  --purge           + пакеты Docker и его репозиторий
  --backups         заодно удалить каталог резервных копий (по умолчанию НЕТ)
  --base-images     заодно удалить скачанные образы caddy/mediamtx/postgres/redis
  -y, --yes         не переспрашивать (для циклов тестирования)
  -h, --help        эта справка

Что НЕ трогается никогда:
  · правило файрвола для SSH;
  · резервные копии (только по флагу --backups);
  · контейнеры и тома других проектов.
HELP
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --status)      MODE="status" ;;
            --reset)       MODE="reset" ;;
            --full)        MODE="full" ;;
            --purge)       MODE="purge" ;;
            --backups)     REMOVE_BACKUPS=1 ;;
            --base-images) REMOVE_BASE_IMAGES=1 ;;
            -y|--yes)      ASSUME_YES=1 ;;
            -h|--help)     usage; exit 0 ;;
            *)             die "неизвестный аргумент «$1». Справка: bash uninstall.sh --help" ;;
        esac
        shift
    done
}

# В режиме --yes все подтверждения считаются данными.
agree() {
    [ "$ASSUME_YES" = "1" ] && return 0
    confirm "$@"
}

have_docker() {
    command -v docker >/dev/null 2>&1
}

count_of() {
    # Печатает количество строк, 0 при пустом выводе.
    local n
    n="$(printf '%s' "$1" | grep -c . || true)"
    printf '%s' "${n:-0}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Что установлено
# ─────────────────────────────────────────────────────────────────────────────
inventory() {
    local containers="" volumes="" networks="" images=""

    if have_docker; then
        containers="$(docker ps -aq --filter "label=$LABEL" 2>/dev/null || true)"
        volumes="$(docker volume ls -q --filter "label=$LABEL" 2>/dev/null || true)"
        networks="$(docker network ls -q --filter "label=$LABEL" 2>/dev/null || true)"
        images="$(docker images -q "$IMAGE" 2>/dev/null || true)"
    fi

    echo "  ${B}Docker${R}"
    if have_docker; then
        printf '    контейнеров: %s\n' "$(count_of "$containers")"
        printf '    томов:       %s' "$(count_of "$volumes")"
        if [ -n "$volumes" ]; then
            printf ' %s(в них база, сертификаты и превью)%s' "$DIM" "$R"
        fi
        printf '\n'
        printf '    сетей:       %s\n' "$(count_of "$networks")"
        printf '    образ %s: %s\n' "$IMAGE" \
            "$( [ -n "$images" ] && echo "есть" || echo "нет" )"
    else
        echo "    не установлен"
    fi

    echo "  ${B}Файлы${R}"
    local f
    for f in .env secrets/app_key backend/app/web/static/vendor/hls.min.js; do
        if [ -e "$f" ]; then
            printf '    %s есть\n' "$(printf '%-46s' "$f")"
        else
            printf '    %s %sнет%s\n' "$(printf '%-46s' "$f")" "$DIM" "$R"
        fi
    done
    local backups
    backups="$(find . -maxdepth 1 -name '.env.backup-*' 2>/dev/null || true)"
    [ -n "$backups" ] && printf '    копий .env: %s\n' "$(count_of "$backups")"

    if [ -d "$BACKUP_DIR_DEFAULT" ]; then
        printf '    %s %s\n' "$(printf '%-46s' "$BACKUP_DIR_DEFAULT")" \
            "$(du -sh "$BACKUP_DIR_DEFAULT" 2>/dev/null | cut -f1 || echo '?')"
    fi

    echo "  ${B}Файрвол${R}"
    if command -v ufw >/dev/null 2>&1; then
        local rules
        rules="$(ufw status 2>/dev/null | grep -E '^(80|443|8189)/' || true)"
        if [ -n "$rules" ]; then
            printf '%s\n' "$rules" | sed 's/^/    /'
        else
            echo "    ${DIM}правил сервиса нет${R}"
        fi
    elif command -v firewall-cmd >/dev/null 2>&1; then
        firewall-cmd --list-ports 2>/dev/null | tr ' ' '\n' \
            | grep -E '^(80|443|8189)/' | sed 's/^/    /' || echo "    ${DIM}правил сервиса нет${R}"
    else
        echo "    ${DIM}ufw/firewalld не установлены${R}"
    fi

    # Читаем .env заново, а не запомненные значения: после удаления файла
    # блок должен исчезнуть, иначе итоговая сводка соврёт.
    if [ -f .env ]; then
        echo "  ${B}Текущая установка${R}"
        printf '    адрес:       %s\n' "${DOMAIN:-?}"
        printf '    сертификат:  %s\n' \
            "$( [ "${TLS_ISSUER:-}" = "internal" ] && echo "свой CA" || echo "Let's Encrypt (${TLS_ISSUER:-?})" )"
    fi
}

load_env_if_any() {
    # -r, а не -f: под обычным пользователем файл с правами 600 не прочитать,
    # и это не повод падать — режим --status должен работать и без root.
    if [ -r .env ]; then
        # shellcheck disable=SC1091
        set -a; . ./.env; set +a
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Шаги удаления
# ─────────────────────────────────────────────────────────────────────────────
offer_backup() {
    step "Резервная копия"

    if [ "$ASSUME_YES" = "1" ]; then
        info "Режим --yes: копия не предлагается."
        return 0
    fi
    if ! have_docker || [ -z "$(docker ps -q --filter "label=$LABEL" --filter "name=postgres" 2>/dev/null || true)" ]; then
        info "База не запущена — копировать нечего."
        return 0
    fi

    warn "Сейчас будут удалены база данных и все настройки камер."
    if confirm "Сделать резервную копию перед удалением?" "да"; then
        if bash ops/backup.sh; then
            ok "Копия создана"
        else
            warn "Копия не создалась."
            confirm "Всё равно продолжить удаление?" "нет" || die "удаление отменено."
        fi
    fi
}

remove_containers() {
    step "Контейнеры, тома и сети"

    if ! have_docker; then
        info "Docker не установлен — пропускаю."
        return 0
    fi

    # Сначала штатный путь. Переменные могут быть уже удалены вместе с .env,
    # поэтому подставляем заглушки — compose требует их только для разбора.
    DOMAIN="${DOMAIN:-placeholder}" \
    TLS_ISSUER="${TLS_ISSUER:-internal}" \
    GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-placeholder}" \
    docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
        down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1 || true

    # Затем зачистка по метке проекта: ловит то, что осталось от прерванных
    # запусков и от старых версий compose-файлов.
    local ids
    ids="$(docker ps -aq --filter "label=$LABEL" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        printf '%s\n' "$ids" | xargs -r docker rm -f >/dev/null 2>&1 || true
    fi
    ids="$(docker volume ls -q --filter "label=$LABEL" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        printf '%s\n' "$ids" | xargs -r docker volume rm -f >/dev/null 2>&1 || true
    fi
    ids="$(docker network ls -q --filter "label=$LABEL" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        printf '%s\n' "$ids" | xargs -r docker network rm >/dev/null 2>&1 || true
    fi

    ok "Контейнеры, тома и сети проекта удалены"
    info "Вместе с ними ушли база, Redis, сертификаты Caddy и превью камер."
}

remove_images() {
    step "Образы"

    if ! have_docker; then
        info "Docker не установлен — пропускаю."
        return 0
    fi

    if [ -n "$(docker images -q "$IMAGE" 2>/dev/null || true)" ]; then
        docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
        ok "Собранный образ $IMAGE удалён"
    else
        info "Собранного образа нет."
    fi

    if [ "$REMOVE_BASE_IMAGES" = "1" ]; then
        local base
        for base in caddy bluenviron/mediamtx postgres redis prom/prometheus grafana/grafana; do
            docker images -q "$base" 2>/dev/null | xargs -r docker rmi -f >/dev/null 2>&1 || true
        done
        ok "Скачанные базовые образы удалены"
    else
        info "Базовые образы оставлены — следующая установка будет быстрее."
        info "Удалить их: bash uninstall.sh --base-images"
    fi
}

remove_files() {
    step "Файлы конфигурации и секреты"

    local removed=0

    if [ -f secrets/app_key ]; then
        warn "Ключ шифрования будет удалён."
        warn "Старые резервные копии базы после этого расшифровать НЕЛЬЗЯ."
        if agree "Точно удалить secrets/app_key?" "да"; then
            rm -f secrets/app_key
            removed=$((removed + 1))
            ok "secrets/app_key удалён"
        else
            info "Ключ оставлен."
        fi
    fi

    local f
    for f in .env /tmp/rtspgw-vendor.log; do
        if [ -e "$f" ]; then
            rm -f "$f"
            removed=$((removed + 1))
        fi
    done

    find . -maxdepth 1 -name '.env.backup-*' -delete 2>/dev/null || true

    if [ -f backend/app/web/static/vendor/hls.min.js ]; then
        rm -f backend/app/web/static/vendor/hls.min.js \
              backend/app/web/static/vendor/hls.min.js.sha256
        removed=$((removed + 1))
        ok "hls.js удалён"
    fi

    ok "Удалено файлов: $removed"

    if [ "$REMOVE_BACKUPS" = "1" ]; then
        if [ -d "$BACKUP_DIR_DEFAULT" ]; then
            warn "Каталог резервных копий $BACKUP_DIR_DEFAULT будет удалён безвозвратно."
            if agree "Удалить резервные копии?" "нет"; then
                rm -rf "${BACKUP_DIR_DEFAULT:?}"
                ok "Резервные копии удалены"
            fi
        fi
    elif [ -d "$BACKUP_DIR_DEFAULT" ]; then
        info "Резервные копии в $BACKUP_DIR_DEFAULT оставлены (флаг --backups удаляет их)."
    fi
}

remove_firewall() {
    step "Правила файрвола"

    if ! agree "Убрать правила для 80/tcp, 443/tcp, 8189/udp и 8189/tcp?" "да"; then
        info "Правила оставлены."
        return 0
    fi

    # Правило SSH не трогаем ни при каких условиях: удалить его по ошибке —
    # значит потерять доступ к серверу.
    if command -v ufw >/dev/null 2>&1; then
        ufw delete allow 80/tcp   >/dev/null 2>&1 || true
        ufw delete allow 443/tcp  >/dev/null 2>&1 || true
        ufw delete allow 8189/udp >/dev/null 2>&1 || true
        ufw delete allow 8189/tcp >/dev/null 2>&1 || true
        ok "ufw: правила сервиса убраны (SSH не тронут)"
    elif command -v firewall-cmd >/dev/null 2>&1; then
        firewall-cmd --permanent --remove-port=80/tcp   >/dev/null 2>&1 || true
        firewall-cmd --permanent --remove-port=443/tcp  >/dev/null 2>&1 || true
        firewall-cmd --permanent --remove-port=8189/udp >/dev/null 2>&1 || true
        firewall-cmd --permanent --remove-port=8189/tcp >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1 || true
        ok "firewalld: правила сервиса убраны (SSH не тронут)"
    else
        info "ufw/firewalld не установлены."
    fi
}

remove_docker() {
    step "Docker"

    if ! have_docker; then
        info "Docker не установлен."
        return 0
    fi

    # Если на машине есть чужие контейнеры или тома, удаление Docker сломает
    # их владельцам жизнь. Проверяем и говорим об этом прямо.
    local all ours others
    all="$(docker ps -aq 2>/dev/null | grep -c . || true)"
    ours="$(docker ps -aq --filter "label=$LABEL" 2>/dev/null | grep -c . || true)"
    others=$(( ${all:-0} - ${ours:-0} ))
    if [ "$others" -gt 0 ]; then
        warn "На машине есть ещё $others контейнер(ов) других проектов."
        warn "Удаление Docker сломает и их."
        agree "Всё равно удалять Docker?" "нет" || { info "Docker оставлен."; return 0; }
    else
        agree "Удалить пакеты Docker и его репозиторий?" "да" || { info "Docker оставлен."; return 0; }
    fi

    systemctl disable --now docker docker.socket >/dev/null 2>&1 || true

    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -qq >/dev/null 2>&1 || true
        rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.gpg
    elif command -v dnf >/dev/null 2>&1; then
        dnf -y -q remove docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 || true
        rm -f /etc/yum.repos.d/docker-ce.repo
    fi
    ok "Пакеты Docker удалены"

    if [ -d /var/lib/docker ]; then
        warn "Каталог /var/lib/docker занимает $(du -sh /var/lib/docker 2>/dev/null | cut -f1 || echo '?')."
        warn "В нём лежат ВСЕ образы и тома этой машины, включая чужие."
        if agree "Удалить /var/lib/docker?" "нет"; then
            rm -rf /var/lib/docker /var/lib/containerd
            ok "Каталоги данных Docker удалены"
        else
            info "Каталог оставлен."
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
summary_uninstall() {
    printf '\n%s' "$B$GRN"
    cat <<'ART'
  ┌─────────────────────────────────────────────┐
  │              Удалено                        │
  └─────────────────────────────────────────────┘
ART
    printf '%s\n' "$R"

    inventory

    echo
    echo "  ${B}Поставить заново:${R}  sudo bash install.sh"

    if [ "$MODE" = "reset" ]; then
        echo
        echo "  ${DIM}.env и ключ шифрования на месте — установщик предложит их${R}"
        echo "  ${DIM}переиспользовать, и вы получите ту же конфигурацию.${R}"
    fi

    echo
    printf '  %sЕсли гоняете установку по кругу с настоящим доменом:%s\n' "$B$YLW" "$R"
    echo "  у Let's Encrypt лимит — 5 сертификатов на один набор имён в неделю."
    echo "  Упереться в него легко, и тогда придётся ждать неделю. Для прогонов"
    echo "  ставьте в .env тестовый CA (сертификат будет недоверенным, лимитов нет):"
    echo
    echo "    ${B}ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory${R}"
    echo
    echo "  Либо тестируйте на режиме со своим CA — он вообще не ходит наружу."
    echo
}

# ─────────────────────────────────────────────────────────────────────────────
main_uninstall() {
    parse_args "$@"

    [ -f docker-compose.yml ] || die "запускайте скрипт из корня проекта."
    # Права root нужны только для удаления; --status ничего не меняет.
    if [ "$MODE" != "status" ] && [ "$(id -u)" -ne 0 ]; then
        die "нужны права root. Запустите: sudo bash uninstall.sh"
    fi

    load_env_if_any

    printf '%s\n' "$B$CYN"
    cat <<'ART'
  ┌─────────────────────────────────────────────┐
  │   RTSP — удаление                   │
  └─────────────────────────────────────────────┘
ART
    printf '%s' "$R"

    echo
    inventory

    if [ "$MODE" = "status" ]; then
        echo
        exit 0
    fi

    if [ -z "$MODE" ]; then
        [ -t 0 ] || die "нет терминала для диалога. Укажите уровень флагом: --reset, --full или --purge."
        echo
        choose MODE "Что удаляем?" \
            "reset:Только данные — контейнеры, база, сертификаты. .env и ключ остаются" \
            "full:Всё, кроме Docker — плюс .env, ключ шифрования, hls.js, правила файрвола" \
            "purge:Всё вместе с Docker"
    fi

    echo
    case "$MODE" in
        reset) warn "Будут удалены контейнеры, база данных, Redis и сертификаты."
               TOTAL_STEPS=3 ;;
        full)  warn "Будет удалено всё, включая .env и ключ шифрования."
               TOTAL_STEPS=5 ;;
        purge) warn "Будет удалено всё, включая пакеты Docker."
               TOTAL_STEPS=6 ;;
    esac
    agree "Продолжить?" "нет" || die "отменено."

    offer_backup
    remove_containers
    remove_images

    case "$MODE" in
        reset)
            info "Режим reset: .env, ключ и правила файрвола оставлены."
            ;;
        full)
            remove_files
            remove_firewall
            ;;
        purge)
            remove_files
            remove_firewall
            remove_docker
            ;;
    esac

    summary_uninstall
}

# Как и install.sh: при `source` отдаём только функции, ничего не удаляя.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main_uninstall "$@"
fi
