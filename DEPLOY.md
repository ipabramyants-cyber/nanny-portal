# 🚀 Инструкция по запуску и деплою — Няня на час

## Быстрый локальный запуск

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Настроить переменные окружения
cp .env.example .env
# Отредактируй .env — заполни TELEGRAM_BOT_TOKEN, ADMIN_IDS

# 3. Запустить
python run.py
# Открыть: http://localhost:5000
```

**Тестовый доступ в админку (dev режим):**  
`http://localhost:5000/admin?dev_token=ВАШ_ADMIN_TOKEN`

---

## Переменные окружения (.env)

| Переменная | Обязательно | Описание |
|---|---|---|
| `FLASK_SECRET_KEY` | ✅ | Секретный ключ сессий (мин. 32 символа, случайный) |
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен бота от @BotFather |
| `ADMIN_IDS` | ✅ | Telegram ID администраторов через запятую (числа!) |
| `ADMIN_TOKEN` | dev | Пароль для Basic Auth доступа в админку |
| `PORT` | нет | Порт сервера (по умолчанию 5000) |
| `FORCE_HTTPS` | прод | Установить `1` на production |
| `STORAGE` | нет | `sql` для PostgreSQL режима |
| `DATABASE_URL` | SQL режим | `postgresql://user:pass@host/db` |
| `SHIFT_TZ` | нет | Таймзона смен, по умолчанию `Asia/Ho_Chi_Minh` |

---

## Как получить ADMIN_IDS

1. Напиши боту [@userinfobot](https://t.me/userinfobot) в Telegram
2. Он ответит твоим числовым ID (например `123456789`)
3. Впиши в `.env`: `ADMIN_IDS=123456789`

---

## Настройка Telegram бота

1. Открой [@BotFather](https://t.me/BotFather)
2. `/newbot` → задай имя и username
3. Скопируй токен → в `TELEGRAM_BOT_TOKEN`
4. Настрой Mini App:
   - `/newapp` или `/setmenubutton` → укажи URL: `https://ТВОЙ_ДОМЕН/app`
5. Включи `inline_mode` если нужно

---

## Продакшн деплой — VPS (Ubuntu)

### 1. Установить зависимости

```bash
sudo apt update && sudo apt install python3-pip nginx certbot python3-certbot-nginx -y
pip3 install -r requirements.txt gunicorn
```

### 2. Настроить systemd сервис

```bash
sudo nano /etc/systemd/system/nanny.service
```

```ini
[Unit]
Description=Nanny Portal Flask App
After=network.target

[Service]
User=www-data
WorkingDirectory=/path/to/nanny_app
EnvironmentFile=/path/to/nanny_app/.env
ExecStart=/usr/local/bin/gunicorn wsgi:app \
    --workers 2 \
    --bind 127.0.0.1:5000 \
    --timeout 60 \
    --access-logfile /var/log/nanny/access.log \
    --error-logfile /var/log/nanny/error.log
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/log/nanny
sudo systemctl daemon-reload
sudo systemctl enable nanny
sudo systemctl start nanny
```

### 3. Nginx конфиг

```bash
sudo nano /etc/nginx/sites-available/nanny
```

```nginx
server {
    listen 80;
    server_name ТВОЙ_ДОМЕН.com;

    client_max_body_size 20M;

    location /static/ {
        alias /path/to/nanny_app/static/;
        expires 7d;
    }

    location /uploads/ {
        alias /path/to/nanny_app/uploads/;
    }

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/nanny /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 4. SSL (HTTPS)

```bash
sudo certbot --nginx -d ТВОЙ_ДОМЕН.com
# Добавь в .env:
# FORCE_HTTPS=1
sudo systemctl restart nanny
```

---

## Деплой на Railway / Render / Fly.io (проще)

### Railway

```bash
# Установить Railway CLI
npm install -g @railway/cli
railway login
railway init
railway up
```

В Railway Dashboard → Variables добавь все переменные из .env

### Render

1. Подключи GitHub репозиторий
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `gunicorn wsgi:app`
4. Добавь Environment Variables

### Fly.io

```bash
fly launch
fly secrets set FLASK_SECRET_KEY=... TELEGRAM_BOT_TOKEN=... ADMIN_IDS=...
fly deploy
```

---

## Привязка домена

### Cloudflare (рекомендуется)

1. Купи домен (namecheap, reg.ru и т.д.)
2. Измени NS-серверы на Cloudflare
3. Добавь A-запись: `@` → IP твоего сервера
4. Включи Proxy (облачко оранжевое)
5. SSL/TLS → Full (strict)

### Напрямую (без Cloudflare)

```
A-запись: @ → IP сервера
A-запись: www → IP сервера
TTL: 300
```

---

## Структура проекта

```
nanny_app/
├── app.py              # Главное Flask приложение
├── run.py              # Dev сервер
├── wsgi.py             # Prod WSGI entry point
├── config.py           # Конфигурация (admin_ids)
├── auth_simple.py      # Авторизация (декораторы)
├── telegram_notify.py  # Отправка Telegram уведомлений
├── telegram_auth.py    # Валидация WebApp initData
├── models.py           # SQLAlchemy модели (SQL режим)
├── requirements.txt    # Зависимости
├── .env                # Настройки (не коммитить!)
├── .env.example        # Шаблон настроек
├── templates/          # Jinja2 шаблоны
├── static/             # CSS, JS, изображения
│   ├── css/style.css
│   ├── js/
│   ├── img/
│   └── contracts/      # PDF договоры
├── data/               # JSON данные (JSON режим)
│   ├── nannies.json
│   ├── leads.json
│   └── reviews.json
└── uploads/            # Загруженные файлы (фото нянь)
```

---

## Частые вопросы

**Как попасть в админку без Telegram?**  
В `.env` установи `ADMIN_TOKEN=секретный_пароль`  
Зайди: `http://сайт/admin?dev_token=секретный_пароль`  
> В production это должен быть сложный пароль!

**Уведомления в Telegram не приходят**  
- Проверь `TELEGRAM_BOT_TOKEN` в .env
- Убедись что у получателя числовой ID (не @username)
- Получатель должен написать боту хоть раз (иначе бот не может отправить)

**Как добавить PDF договоры?**  
Скопируй PDF в `static/contracts/` с именами:
- `contract_agency_parents.pdf`
- `contract_nanny_parents.pdf`  
- `package_clients.pdf`

**Как добавить няню?**  
Зайди в Admin → раздел "Няни" → форма добавления

---

## Маршруты

| URL | Описание |
|---|---|
| `/` | Главная страница |
| `/admin` | Панель администратора |
| `/admin/login` | Вход через Telegram |
| `/client/<token>` | Личный кабинет клиента |
| `/nanny/app` | Кабинет няни |
| `/nanny/<token>` | Публичный профиль няни |
| `/app` | Telegram Mini App entry |
| `/api/lead` | POST — создать заявку |
| `/api/auth/telegram` | POST — Telegram auth |
| `/healthz` | Health check |
