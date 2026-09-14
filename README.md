# VK Alarm Bot — мониторинг VK и форвард в Max

Сервис следит за пабликами VK и публикует **свежие** посты в каналы мессенджера **Max** по расписанию: с фильтрами, обработкой промптом ИИ, копированием медиа, журналом проверок и событиями.

## Возможности

- **Потоки**: список источников VK (ссылки `vk.com`/`vk.ru`, короткие имена, `club123`/`public123`/`id123`, `wall-1_2`) → каналы Max, у каждого потока свой интервал проверки.
- **Курсоры по источникам**: для каждого источника запоминается последний id поста, поэтому при следующей проверке забираются только новые посты.
- **Fresh start**: при снятии с паузы или сохранении рабочих настроек история пропускается — в Max не выливается накопившийся бэклог.
- **Фильтры до ИИ**: минус-слова → обязательные ключевые слова → диапазон ER `(лайки + комменты×5 + репосты×10) / (просмотры + 2000) × 100`.
- **Промпт ИИ**: переписать/перевести/дайджест; маркер SKIP убирает пост из выдачи; при сбое ИИ — публикация оригинала (настраивается).
- **Медиа**: копирование фото/документов из VK в Max (до 10 вложений), ссылка на оригинал в сообщении.
- **Наблюдаемость**: журнал потока (`Проверка запущена` → `Прогресс: N/M источников` → `Проверка завершена за X мин`), события с ER и статусом отправки, оповещения в колокольчике.
- **Безопасность лимитов VK**: глобальный троттлинг ~3 запроса/с, авто-пауза при flood-контроле/капче (в журнале видно `Пауза из-за лимитов VK`).
- **Несколько пользователей**: админ видит все потоки, пользователь — только свои.

## Как это работает

1. Поток запускается (или снимается с паузы) → сервис проходит по источникам и **фиксирует последние id постов** (история не публикуется).
2. Дальше отсчёт интервала от старта проверки; планировщик раз в ~30 с запускает проверки тех потоков, у которых интервал истёк (одновременно идёт только одна проверка).
3. Проверка собирает посты новее зафиксированных id, применяет фильтры и промпт ИИ.
4. Прошедшие посты публикуются в Max в хронологическом порядке, старт/итог проверки пишутся в журнал потока.

## Стек

- **Backend**: FastAPI, SQLAlchemy 2 (async), SQLite (WAL + `busy_timeout`), APScheduler, httpx, pydantic-settings.
- **Frontend**: React + TypeScript + Vite + Tailwind CSS (сборка уже лежит в `frontend/dist`).

## Структура

```
backend/               # FastAPI-приложение
  app/                 # api, сервисы (vk/max/ai), планировщик, модели
  run.py               # локальный запуск (uvicorn, 0.0.0.0:8000, reload)
  requirements.txt
  tests/               # самостоятельные тесты: python tests/test_*.py
frontend/              # SPA (src) + собранный dist/ (отдаётся бэкендом)
deploy/                # systemd-юнит, конфиг nginx, скрипт обновления
.env.example           # шаблон настроек → скопировать в .env
```

## Настройки (файл `.env` в корне проекта)

| Переменная | Обязательно | Описание |
|---|---|---|
| `SECRET_KEY` | да | Секрет для подписи JWT-токенов (длинная случайная строка) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | да | Админ создаётся автоматически при первом старте |
| `VK_SERVICE_TOKEN` | да | Токен VK (сервисный или пользовательский) |
| `MAX_BOT_TOKEN` | да | Токен бота Max для отправки сообщений |
| `AI_PROVIDER` | нет | `gigachat` (по умолчанию) или `openai`-совместимый провайдер |
| `AI_API_KEY` | нет | Ключ ИИ (можно задать позже в «Настройках») |
| `AI_API_BASE` / `AI_MODEL` | нет | Адрес OpenAI-совместимого API и имя модели |
| `GIGACHAT_CREDENTIALS` / `GIGACHAT_SCOPE` / `GIGACHAT_MODEL` | нет | Для GigaChat напрямую |
| `DATABASE_URL` | нет | По умолчанию `sqlite+aiosqlite:///./vk_alarm.db` |

> Токены VK/Max/ИИ можно задать и через интерфейс («Настройки») — они сохраняются в `.env`.
> `.env` в git не попадает (см. `.gitignore`).

## Локальный запуск

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate        Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example ../.env      # Windows: copy .env.example ..\.env
# заполните .env (SECRET_KEY, пароли, токены VK/Max)

python run.py                # http://127.0.0.1:8000
```

Фронтенд уже собран в `frontend/dist` и отдаётся бэкендом. Если меняли UI:

```bash
cd frontend
npm install
npm run build
```

Тесты (запускать как скрипты, не через pytest):

```bash
cd backend
python tests/test_er_and_filters.py
python tests/test_wall_pagination.py
python tests/test_max_service.py
```

## Запуск на сервере (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install -y python3-venv git nginx

sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/Vepsnoid/vk-alarm-bot.git
cd /opt/vk-alarm-bot

python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

cp .env.example .env
nano .env                      # SECRET_KEY, ADMIN_*, VK_SERVICE_TOKEN, MAX_BOT_TOKEN, ключ ИИ

# сервис работает от www-data и должен писать базу в backend/
sudo chown -R www-data:www-data /opt/vk-alarm-bot

sudo cp deploy/vk-alarm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vk-alarm
systemctl status vk-alarm --no-pager
curl http://127.0.0.1:8000/api/health      # ожидаем {"status":"ok", ...}
```

Домен и HTTPS через nginx:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/vk-alarm
sudo nano /etc/nginx/sites-available/vk-alarm      # указать server_name (ваш домен)
sudo ln -s /etc/nginx/sites-available/vk-alarm /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx
```

Обновление версии на сервере:

```bash
sudo /opt/vk-alarm-bot/deploy/deploy.sh
```

## Данные и бэкапы

- База: `backend/vk_alarm.db` (SQLite). В режиме WAL рядом лежат `vk_alarm.db-wal` и `vk_alarm.db-shm` — при копировании берите все три файла (или остановите сервис).
- Секреты: только в `.env` на сервере, в git их нет.
- Логи systemd: `journalctl -u vk-alarm -f`.
