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
SCRIPT_HASH_BEFORE="$(md5sum "$0" 2>/dev/null | cut -d' ' -f1 || true)"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
echo "    версия: $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"

# Этот же git pull мог обновить сам deploy.sh, а bash уже держит старую версию
# скрипта в памяти и дочитает её из буфера — тогда обновление прошло бы по прежней
# логике (именно так один раз вылезло ложное «Failed to connect»). Поэтому
# перезапускаем себя новой версией; маркер в окружении защищает от рекурсии.
SCRIPT_HASH_AFTER="$(md5sum "$0" 2>/dev/null | cut -d' ' -f1 || true)"
if [[ -n "${SCRIPT_HASH_BEFORE}" && "${SCRIPT_HASH_BEFORE}" != "${SCRIPT_HASH_AFTER}" && -z "${VK_ALARM_DEPLOY_REEXEC:-}" ]]; then
  echo "==> deploy.sh обновился этим pull — перезапускаю его новой версией"
  VK_ALARM_DEPLOY_REEXEC=1 exec bash "$0" "$@"
fi

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

# Приложению нужно несколько секунд на старт (ру migrations + подсчёт хешей), поэтому
# проверяем API с повторами: одиночный curl через 2 с после restart давал ложное
# «Failed to connect to 127.0.0.1 port 8000», хотя сервис поднимался нормально.
echo "==> Проверка API (до 60 с)"
health=""
for _ in $(seq 1 30); do
  if health="$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/health 2>/dev/null)"; then
    break
  fi
  health=""
  sleep 2
done

if [[ -n "${health}" ]]; then
  echo "    API отвечает: ${health}"
  echo "Готово."
else
  echo "ОШИБКА: API не ответил за 60 с (systemctl is-active: $(systemctl is-active "$SERVICE" 2>/dev/null || true))" >&2
  echo "    последние строки журнала:" >&2
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  echo "    частые причины: пустые SECRET_KEY/ADMIN_USERNAME/ADMIN_PASSWORD в .env," >&2
  echo "    значение MAX_NEW_POSTS_PER_RUN/MAX_RETRIES_PER_RUN вне диапазона или ошибка в токенах." >&2
  exit 1
fi
