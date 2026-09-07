#!/usr/bin/env bash
# Обновление прода до текущей версии кода — одной командой.
#
#     ./ops/update.sh                # git pull + дамп + сборка + миграции + проверка
#     ./ops/update.sh --no-pull      # код уже на месте (правили прямо на сервере)
#     ./ops/update.sh --no-backup    # без дампа БД (быстрее, но откатываться нечем)
#     ./ops/update.sh --no-rollback  # не откатывать код, если сервис не поднялся
#     ./ops/update.sh --rollback     # вернуть предыдущую версию кода прямо сейчас
#
# Смысл скрипта — чтобы его можно было запускать не думая. Поэтому здесь есть
# три вещи, которых не было, пока это был просто «pull + build + upgrade»:
#
#   1. Дамп БД снимается ДО миграций. Миграция необратима на практике: у
#      alembic есть downgrade, но он теряет данные, и полагаться на него в
#      три часа ночи нельзя. Дамп — это то, из чего действительно можно
#      восстановиться.
#   2. Готовность проверяется по-настоящему. Раньше проверка заканчивалась
#      на `|| true`, то есть сломанное обновление выглядело точно так же, как
#      удачное, и узнавали о нём от пользователей.
#   3. Если сервис не поднялся — код откатывается сам. Схему при этом НЕ
#      трогаем: старый код с новой схемой сервис переживает (schema_check это
#      обрабатывает), а вот downgrade посреди аварии — способ потерять данные.
#
# Порядок шагов тоже не случайный: сборка идёт раньше, чем останавливается
# что-либо работающее. Сломанный Dockerfile или синтаксическая ошибка уронят
# скрипт до того, как прод будет тронут.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

#: Сколько ждём, пока сервис отчитается о готовности. Холодный старт api —
#: около 20 секунд, миграции и прогрев MediaMTX добавляют ещё; 120 с берём
#: с запасом, чтобы не объявлять аварию там, где просто медленный диск.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"

DO_PULL=1
DO_BACKUP=1
DO_ROLLBACK=1
ROLLBACK_NOW=0

for arg in "$@"; do
    case "$arg" in
        --no-pull) DO_PULL=0 ;;
        --no-backup) DO_BACKUP=0 ;;
        --no-rollback) DO_ROLLBACK=0 ;;
        --rollback) ROLLBACK_NOW=1 ;;
        -h|--help) sed -n '2,8p' "$0" | cut -c3-; exit 0 ;;
        *) echo "Неизвестный аргумент: $arg (см. --help)" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

# Мониторинг поднят, если install.sh записал в .env пароль Grafana. Без этого
# флага `up -d` посчитал бы prometheus и grafana чужими контейнерами и погасил
# бы их.
COMPOSE_FILES=(-f docker-compose.yml)
if [ -f .env ] && grep -q '^GRAFANA_PASSWORD=' .env; then
    COMPOSE_FILES+=(-f docker-compose.monitoring.yml)
fi

compose() { docker compose "${COMPOSE_FILES[@]}" "$@"; }

# ─── Проверки до того, как что-то трогать ────────────────────────────────────
preflight() {
    say "Проверяю окружение"
    command -v docker >/dev/null 2>&1 || die "docker не найден"
    docker compose version >/dev/null 2>&1 || die "нужен docker compose v2"
    [ -f .env ] || die ".env не найден — это точно каталог установки?"
    [ -s secrets/app_key ] \
        || die "secrets/app_key пуст или отсутствует. Без ключа шифрования сервис не поднимется"

    # Битый compose или .env лучше поймать здесь, чем на середине обновления.
    compose config --quiet || die "docker-compose.yml или .env не проходят проверку"
    echo "Окружение в порядке."
}

# Сохранённая точка возврата. Пишем в файл, а не только в переменную: если
# скрипт прервут на середине, откатываться всё равно будет к чему.
STATE_FILE=".update-previous-revision"

current_revision() { git rev-parse HEAD 2>/dev/null || true; }

is_git_checkout() { [ -d .git ]; }

working_tree_is_clean() { [ -z "$(git status --porcelain 2>/dev/null)" ]; }

# ─── Откат ───────────────────────────────────────────────────────────────────
rollback_to() {
    local revision="$1"
    # git reset --hard стирает несохранённые правки. Если кто-то правил файлы
    # прямо на сервере (а с --no-pull это ровно тот случай), молча снести его
    # работу ради отката — плохой размен: пусть решает человек.
    working_tree_is_clean || die "в рабочем каталоге есть незакоммиченные изменения —
     откат их сотрёт. Сохраните их (git stash) и повторите: ./ops/update.sh --rollback"
    say "Откатываю код на $revision"
    git reset --hard "$revision"
    compose build
    compose up -d
    warn "Код откачен. Схема БД НЕ откатывалась — она осталась новой."
    warn "Это рабочее состояние: сервис переживает схему впереди кода."
    warn "Если нужно вернуть и схему — восстанавливайте из дампа, см. ops/restore.sh."
}

if [ "$ROLLBACK_NOW" = 1 ]; then
    preflight
    is_git_checkout || die "не git-каталог — откатывать нечем"
    [ -s "$STATE_FILE" ] || die "нет записи о предыдущей версии ($STATE_FILE)"
    rollback_to "$(cat "$STATE_FILE")"
    exit 0
fi

# ─── Обновление ──────────────────────────────────────────────────────────────
preflight

PREVIOUS=""
if is_git_checkout; then
    PREVIOUS="$(current_revision)"
    [ -n "$PREVIOUS" ] && printf '%s\n' "$PREVIOUS" > "$STATE_FILE"
fi

if [ "$DO_PULL" = 0 ]; then
    say "Код не трогаю (--no-pull)"
elif ! is_git_checkout; then
    warn "Это не git-каталог — код обновите сами, дальше только сборка."
elif ! working_tree_is_clean; then
    git status --short >&2
    die "в рабочем каталоге есть незакоммиченные изменения.
     Закоммитьте их, отмените (git checkout -- .) — либо запустите с --no-pull,
     если правили прямо здесь и хотите собрать как есть."
else
    say "Забираю свежий код"
    git pull --ff-only
fi

# Запущен ли postgres. Через `ps -q` + inspect, а не через `ps --status`:
# последний появился не во всех сборках compose v2, и на старой отсутствие
# флага выглядело бы как «базы нет» — дамп молча пропускался бы.
postgres_running() {
    local cid
    cid="$(compose ps -q postgres 2>/dev/null || true)"
    [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ]
}

# Каталог дампов. По умолчанию /var/backups/rtspgw, но туда пишет только root,
# а обновление часто запускают обычным пользователем из группы docker. Падать
# на этом нельзя: получилось бы, что защита от потери данных мешает
# обновляться, и её начнут отключать флагом.
if [ -z "${BACKUP_DIR:-}" ]; then
    if mkdir -p /var/backups/rtspgw 2>/dev/null && [ -w /var/backups/rtspgw ]; then
        BACKUP_DIR=/var/backups/rtspgw
    else
        BACKUP_DIR="$PROJECT_DIR/ops/backups"
    fi
fi
export BACKUP_DIR

# Дамп до миграций: после них восстановиться уже не из чего.
if [ "$DO_BACKUP" = 0 ]; then
    warn "Дамп БД пропущен (--no-backup) — откатить миграцию будет нечем."
elif ! postgres_running; then
    warn "postgres не запущен — дамп пропускаю (похоже на первую установку)."
else
    say "Снимаю дамп БД в $BACKUP_DIR"
    bash ./ops/backup.sh || die "дамп не снялся. Обновление остановлено — это защита,
     а не придирка: без дампа неудачную миграцию нечем откатывать.
     Осознанно продолжить: ./ops/update.sh --no-backup"
fi

# Сборка первой: сломанный Dockerfile уронит нас здесь, пока прод ещё работает.
say "Собираю образы"
compose build

# Постгрес нужен живым для миграций; поднимаем только хранилища, чтобы новый
# код ещё не начал обслуживать запросы на старой схеме.
say "Поднимаю хранилища"
compose up -d postgres redis

say "Накатываю миграции"
compose run --rm api alembic upgrade head

say "Перезапускаю сервисы"
compose up -d

# ─── Проверка, что оно действительно поднялось ───────────────────────────────
readyz_body() {
    compose exec -T api python -c \
        "import httpx;print(httpx.get('http://127.0.0.1:8000/readyz').text)" 2>/dev/null || true
}

say "Жду готовности (до $HEALTH_TIMEOUT с)"
deadline=$((SECONDS + HEALTH_TIMEOUT))
ready=0
body=""
while [ "$SECONDS" -lt "$deadline" ]; do
    body="$(readyz_body)"
    # Starlette печатает JSON без пробелов, но подстраховаться дешевле.
    case "${body// /}" in
        *'"ready":true'*) ready=1; break ;;
    esac
    sleep 3
done

if [ "$ready" = 1 ]; then
    say "Готово"
    echo "$body"
    compose ps
    if [ -n "$PREVIOUS" ]; then
        echo
        echo "Что приехало:"
        git --no-pager log --oneline "$PREVIOUS..HEAD" | sed 's/^/  /' || true
    fi
    # Старые слои копятся с каждой сборкой и однажды забивают диск. Сутки
    # выдержки — чтобы не снести образ, к которому может понадобиться откат.
    docker image prune -f --filter "until=24h" >/dev/null 2>&1 || true
    exit 0
fi

# ─── Не поднялось ────────────────────────────────────────────────────────────
warn ""
warn "Сервис не отчитался о готовности за $HEALTH_TIMEOUT с."
warn "Последний ответ /readyz: ${body:-(пусто)}"
warn ""
echo "Состояние контейнеров:" >&2
compose ps >&2 || true
echo >&2
echo "Последние строки лога api:" >&2
compose logs --tail=40 api >&2 || true

if [ "$DO_ROLLBACK" = 0 ]; then
    die "Откат отключён (--no-rollback). Разбирайтесь по логу выше."
fi
if [ -z "$PREVIOUS" ]; then
    die "Откатывать не к чему (не git-каталог). Разбирайтесь по логу выше."
fi
if [ "$PREVIOUS" = "$(current_revision)" ]; then
    die "Код и так не менялся — дело не в новой версии. Разбирайтесь по логу выше."
fi

rollback_to "$PREVIOUS"
die "Обновление не удалось, код возвращён на предыдущую версию.
     Причина — в логе выше. Дамп БД: $BACKUP_DIR"
