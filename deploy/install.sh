#!/usr/bin/env bash
# Установка VK Alarm Bot на Ubuntu/Debian одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/Vepsnoid/vk-alarm-bot/main/deploy/install.sh | sudo bash
#
# Переменные (необязательно):
#   APP_DIR=/opt/vk-alarm-bot  REPO=https://github.com/Vepsnoid/vk-alarm-bot.git
#   SERVICE=vk-alarm           RUN_USER=www-data
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vk-alarm-bot}"
REPO="${REPO:-https://github.com/Vepsnoid/vk-alarm-bot.git}"
SERVICE="${SERVICE:-vk-alarm}"
RUN_USER="${RUN_USER:-www-data}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите через sudo (или curl | sudo bash)." >&2
  exit 1
fi

echo "==> Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git nginx curl ca-certificates

if [[ -d "${APP_DIR}/.git" ]]; then
  echo "==> ${APP_DIR} уже есть — обновляю код"
  git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
  git -C "${APP_DIR}" pull --ff-only
else
  echo "==> Клонирую репозиторий в ${APP_DIR}"
  mkdir -p "$(dirname "${APP_DIR}")"
  git clone "${REPO}" "${APP_DIR}"
fi
cd "${APP_DIR}"

echo "==> Виртуальное окружение и зависимости Python"
python3 -m venv backend/.venv
backend/.venv/bin/pip install -q --upgrade pip
backend/.venv/bin/pip install -q -r backend/requirements.txt

if [[ ! -f .env ]]; then
  echo "==> Создаю .env из шаблона (SECRET_KEY сгенерирован)"
  cp .env.example .env
  secret="$(openssl rand -hex 32 2>/dev/null || (head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'))"
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" .env
else
  echo "==> .env уже существует — оставляю без изменений"
fi

echo "==> Права на каталог (сервис работает от ${RUN_USER})"
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"

echo "==> systemd-сервис ${SERVICE}"
install -m 644 deploy/vk-alarm.service "/etc/systemd/system/${SERVICE}.service"
if [[ "${APP_DIR}" != "/opt/vk-alarm-bot" ]]; then
  sed -i "s|/opt/vk-alarm-bot|${APP_DIR}|g" "/etc/systemd/system/${SERVICE}.service"
fi
systemctl daemon-reload
systemctl enable --now "${SERVICE}"
sleep 2
systemctl --no-pager --lines=8 status "${SERVICE}" || true

echo "==> Проверка API"
curl -fsS http://127.0.0.1:8000/api/health && echo || {
  echo "API не ответил — смотрите: journalctl -u ${SERVICE} -n 50" >&2
}

cat <<TXT

Готово. Осталось:
  1) заполнить настройки:  sudo nano ${APP_DIR}/.env   (пароль админа, токены VK/Max, ключ ИИ)
  2) перезапустить:        sudo systemctl restart ${SERVICE}
  3) проверить:            curl http://127.0.0.1:8000/api/health
  4) открыть интерфейс:    http://<IP-сервера>:8000   (или настроить nginx: deploy/nginx.conf)

Логин по умолчанию — ADMIN_USERNAME / ADMIN_PASSWORD из .env.
Обновление в будущем: sudo ${APP_DIR}/deploy/deploy.sh
TXT
