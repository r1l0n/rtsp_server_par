#!/usr/bin/env bash
# Обновление до текущей версии кода.
#
#     ./ops/update.sh              # git pull + пересборка + миграции
#     ./ops/update.sh --no-pull    # то же, но код уже на месте
#
# Шага три, и порядок важен: сначала образы, потом схема базы. Руками это
# делается теми же командами, но забытый `alembic upgrade head` выглядит потом
# не как забытая команда, а как случайная пятисотка на одной форме — код уже
# знает про таблицу, которой в базе ещё нет.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

FILES=(-f docker-compose.yml)
# Мониторинг поднят, если install.sh записал в .env пароль Grafana. Без этого
# флага `up -d` посчитал бы prometheus и grafana чужими контейнерами.
if [ -f .env ] && grep -q '^GRAFANA_PASSWORD=' .env; then
    FILES+=(-f docker-compose.monitoring.yml)
fi

if [ "${1:-}" = "--no-pull" ]; then
    echo "Код не трогаю (--no-pull)."
elif [ -d .git ]; then
    echo "Забираю свежий код..."
    git pull --ff-only
else
    echo "Это не git-каталог — код обновите сами, дальше только сборка." >&2
fi

echo "Собираю образы и перезапускаю контейнеры..."
docker compose "${FILES[@]}" up -d --build

# Миграции идут после запуска: `run --rm api` поднимает отдельный контейнер,
# которому нужен уже здоровый postgres из общей сети.
echo "Накатываю миграции..."
docker compose "${FILES[@]}" run --rm api alembic upgrade head

echo
echo "Состояние:"
docker compose "${FILES[@]}" ps

echo
echo "Готовность (сразу после перезапуска может быть 503 — контейнер поднимается ~20 с):"
docker compose "${FILES[@]}" exec -T api \
    python -c "import httpx;print(httpx.get('http://127.0.0.1:8000/readyz').text)" || true
