# Конфигурация CRM-бота (переопределяется через .env)
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

API_TOKEN = os.environ.get("API_TOKEN") or "8404693091:AAEGJlbIy-toCi5tOqRllt5o1P3oRHkFyPE"
ID_INSTANCE = os.environ.get("ID_INSTANCE") or "7103499086"
API_TOKEN_INSTANCE = os.environ.get("API_TOKEN_INSTANCE") or "c143271a593d461a9bef407fcaaedca3e2c4268346f143f3b8"
API_URL = (os.environ.get("API_URL") or "https://7103.api.greenapi.com").strip().rstrip("/")
MED_ID_INSTANCE = (os.environ.get('MED_ID_INSTANCE') or '7103507365').strip()
MED_API_TOKEN = (os.environ.get('MED_API_TOKEN') or '925f590eb2a24be9a462321974bca84fd53e067da54149d098').strip()
MED_API_URL = (os.environ.get('MED_API_URL') or 'https://7103.api.greenapi.com').strip().rstrip('/')
DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm_base.db"))
OWNER_ID = int(os.environ.get("OWNER_ID") or "6428583782")

_db_dir = os.path.dirname(DB_PATH)
if _db_dir and not os.path.isdir(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

DOCTOR_LIMITS = {"assistant": 10, "ganchina": 5}
CHATTING_IDLE_MINUTES = 20
REMINDER_24H_HOURS = 24
REMINDER_24H_WINDOW_HOURS = 48

# Telegram-аккаунт для приёма лидов (MTProto; опционально)
TELEGRAM_LEADS_PHONE = os.environ.get("TELEGRAM_LEADS_PHONE", "").strip()  # например +992877631000
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_PATH = os.environ.get("TELEGRAM_SESSION_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_leads_session"))

# Google Sheets — база лидов для SMS-рассылки (опционально)
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "176UpFHBawww6QJroJFFkImNYSepaSsjyUtYXQJdsY7c").strip()
GOOGLE_SHEET_TAB_NAME = os.environ.get("GOOGLE_SHEET_TAB_NAME", "База данных лидов")
# JSON ключа сервисного аккаунта. Задай в .env или в переменных Amvera — иначе запись в таблицу отключена.
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
