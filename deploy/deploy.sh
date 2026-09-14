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

echo "==> Обновляю код в $APP_DIR ($BRANCH)"
cd "$APP_DIR"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> Ставлю зависимости Python"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r backend/requirements.txt

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
