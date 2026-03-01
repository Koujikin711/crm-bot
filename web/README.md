# Веб-интерфейс Metodi CRM

Тестовый сайт: **вход через Telegram** и **боковое меню по ролям** (как в Битрикс).

## Запуск (из корня проекта CRM)

```bash
# Установить зависимости веба (если ещё не стоят)
pip install -r web/requirements.txt

# Запуск на порту 5000
python -m web.app
```

Или через Flask CLI:

```bash
cd C:\Users\user\Desktop\CRM
set FLASK_APP=web.app
flask run --host 0.0.0.0 --port 5000
```

Открой в браузере: **http://localhost:5000**

## Настройка

- **Бот для входа:** используется тот же `API_TOKEN` из `config.py` / `.env`. Имя бота для виджета — переменная `BOT_USERNAME` (по умолчанию `MetodiCRM_bot`). При переносе на другой хост задай в `.env`: `BOT_USERNAME=ТвойБот`.
- **Секрет сессий:** для продакшена задай в `.env`: `WEB_SECRET_KEY=случайная-длинная-строка`.
- **Домен для Telegram Login:** при переносе на хост укажи в настройках бота @BotFather домен сайта (Settings → Domain).

## Перенос на отдельный хост

1. Скопируй папку `web/` и при необходимости `config.py`, `.env`, `crm_base.db` (или укажи `CRM_DB_PATH` на удалённую БД).
2. На сервере: `pip install -r web/requirements.txt`, запуск через gunicorn или аналог:  
   `gunicorn -w 1 -b 0.0.0.0:5000 "web.app:app"`.
3. Настрой HTTPS (Let's Encrypt) и в BotFather укажи домен для виджета.
