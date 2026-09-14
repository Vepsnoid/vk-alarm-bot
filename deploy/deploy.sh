#!/usr/bin/env bash
# Обновление VK Alarm Bot на сервере: git pull → зависимости → перезапуск.
#
#   sudo /opt/vk-alarm-bot/deploy/deploy.sh
#
# Переменные можно переопределить: APP_DIR=/opt/vk-alarm-bot SERVICE=vk-alarm ./deploy.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vk-alarm-bot}"
SERVICE="${SERVICE:-vk-alarm}"
VENV="${VENV:-$APP_DIR/backend/.venv}"
BRANCH="${BRANCH:-main}"
RUN_USER="${RUN_USER:-www-data}"

echo "==> Обновляю код в $APP_DIR ($BRANCH)"
cd "$APP_DIR"
# Каталог принадлежит www-data, а команду запускают через sudo — без этой записи
# git откажется работать («detected dubious ownership»).
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> Ставлю зависимости Python"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r backend/requirements.txt

# git pull под root создаёт файлы от root — возвращаем каталог сервисному пользователю
# (он должен уметь писать базу SQLite в backend/).
echo "==> Права на каталог ($RUN_USER)"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

# Фронтенд уже собран в репозитории (frontend/dist). Если собираете на сервере —
# установите Node.js и раскомментируйте блок ниже.
# echo "==> Собираю фронтенд"
# (cd frontend && npm ci && npm run build)

echo "==> Перезапускаю сервис $SERVICE"
systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --lines=10 status "$SERVICE" || true

echo "==> Проверка API"
curl -fsS http://127.0.0.1:8000/api/health && echo
echo "Готово."
