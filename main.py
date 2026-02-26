import logging
import asyncio
import json
import aiohttp
import sqlite3
import os
import pandas as pd
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- КОНФИГУРАЦИЯ (переопределяется через .env или переменные окружения) ---
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

API_TOKEN = os.environ.get("API_TOKEN") or "8404693091:AAEGJlbIy-toCi5tOqRllt5o1P3oRHkFyPE"
ID_INSTANCE = os.environ.get("ID_INSTANCE") or "7103499086"
API_TOKEN_INSTANCE = os.environ.get("API_TOKEN_INSTANCE") or "c143271a593d461a9bef407fcaaedca3e2c4268346f143f3b8"
API_URL = (os.environ.get("API_URL") or "https://7103.api.greenapi.com").strip().rstrip("/")
# Медицина (второй инстанс Green API). Телефон: 992877631000
# Можно переопределить через env: MED_ID_INSTANCE, MED_API_TOKEN, MED_API_URL
MED_ID_INSTANCE = (os.environ.get('MED_ID_INSTANCE') or '7103507365').strip()
MED_API_TOKEN = (os.environ.get('MED_API_TOKEN') or '925f590eb2a24be9a462321974bca84fd53e067da54149d098').strip()
MED_API_URL = (os.environ.get('MED_API_URL') or 'https://7103.api.greenapi.com').strip().rstrip('/')

# Путь к БД: задайте CRM_DB_PATH в окружении (напр. /data/crm_base.db)
DB_PATH = os.environ.get('CRM_DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crm_base.db'))
_db_dir = os.path.dirname(DB_PATH)
if _db_dir and not os.path.isdir(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)
OWNER_ID = int(os.environ.get("OWNER_ID") or "6428583782")

# Глобальная aiohttp-сессия для Green API (создаётся в main())
g_http_session: aiohttp.ClientSession = None

async def _wa_get(url):
    """Асинхронный GET к Green API. Возвращает (status_code, text, json или None)."""
    if not g_http_session:
        return 0, "", None
    try:
        async with g_http_session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text.strip() else None
            except Exception:
                data = None
            return resp.status, text, data
    except asyncio.TimeoutError:
        raise
    except Exception as e:
        logging.warning("_wa_get %s: %s", url[:80], e)
        return 0, "", None

async def _wa_delete(url):
    if not g_http_session:
        return
    try:
        async with g_http_session.delete(url, timeout=aiohttp.ClientTimeout(total=10)) as _:
            pass
    except Exception as e:
        logging.warning("_wa_delete %s: %s", url[:80], e)

async def _wa_post(url, json_data):
    if not g_http_session:
        return
    try:
        async with g_http_session.post(url, json=json_data, timeout=aiohttp.ClientTimeout(total=15)) as _:
            pass
    except Exception as e:
        logging.warning("_wa_post %s: %s", url[:80], e)

def is_owner(user_id: int) -> bool:
    row = execute_query("SELECT 1 FROM users WHERE user_id = ? AND role = 'owner'", (user_id,), fetchone=True)
    return bool(row)

def get_first_owner_id() -> int:
    row = execute_query("SELECT user_id FROM users WHERE role = 'owner' ORDER BY user_id LIMIT 1", fetchone=True)
    return row[0] if row else OWNER_ID

def get_all_owner_ids():
    """Список ID всех владельцев — для рассылки заявок."""
    rows = execute_query("SELECT user_id FROM users WHERE role = 'owner' ORDER BY user_id", fetchall=True)
    return [r[0] for r in rows] if rows else [OWNER_ID]

async def safe_callback_answer(c: types.CallbackQuery, text: str = None):
    """Ответ на callback; игнорируем «query is too old», чтобы не падать при долгой обработке."""
    try:
        await c.answer(text=text)
    except TelegramBadRequest as e:
        if "query is too old" not in str(e).lower() and "query id is invalid" not in str(e).lower():
            raise

async def try_assign_queued_lead_to_manager(manager_id: int, direction: str):
    """После закрытия лида: если есть лид в очереди (status=pending), назначить его этому менеджеру."""
    if direction == 'med':
        cond = "direction = 'med'"
    else:
        cond = "(direction = 'biz' OR direction IS NULL)"
    row = execute_query(
        f"SELECT phone, name FROM leads WHERE status = 'pending' AND {cond} ORDER BY last_touch ASC LIMIT 1",
        (),
        fetchone=True,
    )
    if not row:
        return
    phone, name = row[0], row[1] or phone
    execute_query("UPDATE leads SET manager_id = ?, status = 'active' WHERE phone = ?", (manager_id, phone))
    execute_query("UPDATE users SET is_busy = 1 WHERE user_id = ?", (manager_id,))
    kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone}").adjust(2).as_markup()
    await bot.send_message(manager_id, f"📥 <b>Следующий лид из очереди</b>\n👤 {name}\n📞 {phone}", reply_markup=kb, parse_mode="HTML")

def get_free_manager_for_direction(direction: str):
    """Менеджер свободен: is_busy=0 И нет лидов active/chatting за ним."""
    if direction == 'med':
        role_cond = "LOWER(role)='manager' AND sphere='med'"
        lead_cond = "direction = 'med'"
    else:
        role_cond = "(LOWER(role)='manager' AND (sphere='biz' OR sphere IS NULL)) OR (LOWER(role)='admin' AND (sphere='biz' OR sphere IS NULL) AND COALESCE(can_receive_leads,0)=1)"
        lead_cond = "(direction = 'biz' OR direction IS NULL)"
    row = execute_query(
        f"""SELECT u.user_id FROM users u
            WHERE ({role_cond}) AND COALESCE(u.is_busy, 0) = 0
            AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.manager_id = u.user_id AND l.status IN ('active', 'chatting') AND ({lead_cond}))
            ORDER BY u.user_id LIMIT 1""",
        (),
        fetchone=True,
    )
    return row[0] if row else None

# Чтобы наши логи точно были в выводе Amvera (и в консоли)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [CRM] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    waiting_for_reply = State()
    closing_sphere = State()
    closing_comment = State()
    reg_fio = State()
    new_chat_phone = State()
    new_chat_media = State()
    new_chat_sphere = State()  # куда слать: biz | med
    setting_plan_value = State()
    # Медицина
    med_appoint_date = State()
    med_appoint_time = State()
    med_appoint_phone = State()
    med_payment_phone = State()
    med_payment_sum = State()
    med_extend_phone = State()
    med_admin_username = State()
    med_income_period = State()
    # Бизнес: поступления лидов (кастомная дата/период)
    biz_leads_custom_period = State()
    # Медицина: поступления лидов (кастомная дата/период)
    med_leads_custom_period = State()
    # Медицина: завершение диалога -> Оплатил -> выбор пакета и суммы
    med_paid_sum = State()
    # Удобство: поиск лида (ввод номера или ФИО)
    lead_search_query = State()

# --- БАЗА ДАННЫХ ---
def execute_query(query, params=(), fetchone=False, fetchall=False):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        cur = conn.cursor()
        cur.execute(query, params)
        if fetchone: return cur.fetchone()
        if fetchall: return cur.fetchall()
        conn.commit()

def init_db():
    execute_query('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, role TEXT, is_busy INTEGER DEFAULT 0, plan INTEGER DEFAULT 0, fio TEXT, sphere TEXT, can_receive_leads INTEGER DEFAULT 0)')
    execute_query('''CREATE TABLE IF NOT EXISTS leads 
                      (phone TEXT PRIMARY KEY, name TEXT, status TEXT, 
                       manager_id INTEGER, sphere TEXT, comment TEXT, touches INTEGER DEFAULT 0, 
                       last_touch DATETIME, is_answered INTEGER DEFAULT 0,
                       direction TEXT, payment REAL DEFAULT 0, debt REAL DEFAULT 0,
                       service TEXT, payment_date TEXT, massage_sessions INTEGER DEFAULT 0)''')
    execute_query('CREATE TABLE IF NOT EXISTS auth_queue (phone TEXT PRIMARY KEY, step INTEGER DEFAULT 0, instance_sphere TEXT)')
    execute_query('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    execute_query('''CREATE TABLE IF NOT EXISTS appointments 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, doctor TEXT, phone TEXT)''')
    execute_query('CREATE TABLE IF NOT EXISTS pending_reg (user_id INTEGER PRIMARY KEY, fio TEXT)')
    execute_query('''CREATE TABLE IF NOT EXISTS lead_events
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, event_type TEXT, event_data TEXT, created_at TEXT, user_id INTEGER)''')
    execute_query('''CREATE TABLE IF NOT EXISTS reminded_24h (phone TEXT, sphere TEXT, reminded_at TEXT, PRIMARY KEY (phone, sphere))''')
    execute_query('''CREATE TABLE IF NOT EXISTS follow_up_queue (phone TEXT PRIMARY KEY, direction TEXT, created_at TEXT, last_message TEXT)''')
    execute_query('''CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message_id INTEGER, phone TEXT, created_at TEXT, context TEXT)''')
    execute_query('''CREATE TABLE IF NOT EXISTS chat_sessions (user_id INTEGER, phone TEXT PRIMARY KEY, last_outgoing_at TEXT, reminder_sent INTEGER DEFAULT 0)''')
    try: execute_query("ALTER TABLE chat_history ADD COLUMN context TEXT")
    except: pass
    try: execute_query("ALTER TABLE chat_sessions ADD COLUMN reminder_sent INTEGER DEFAULT 0")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN direction TEXT")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN payment REAL DEFAULT 0")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN debt REAL DEFAULT 0")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN service TEXT")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN payment_date TEXT")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN massage_sessions INTEGER DEFAULT 0")
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN sphere TEXT")
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN can_receive_leads INTEGER DEFAULT 0")
    except: pass
    try: execute_query("ALTER TABLE auth_queue ADD COLUMN instance_sphere TEXT")
    except: pass
    try: execute_query("ALTER TABLE leads ADD COLUMN is_answered INTEGER DEFAULT 0")
    except: pass
    execute_query("INSERT OR REPLACE INTO users (user_id, role, fio, sphere) VALUES (?, 'owner', 'Фаридун', NULL)", (OWNER_ID,))
    execute_query("INSERT OR IGNORE INTO settings (key, value) VALUES ('leads_enabled', '1')")

def _wa_urls(sphere):
    """URLs для отправки в WA: sphere = 'biz' | 'med'."""
    if sphere == 'med':
        base = MED_API_URL
        iid, tok = MED_ID_INSTANCE, MED_API_TOKEN
    else:
        base, iid, tok = API_URL, ID_INSTANCE, API_TOKEN_INSTANCE
    return (
        f"{base}/waInstance{iid}/sendMessage/{tok}",
        f"{base}/waInstance{iid}/sendFileByUrl/{tok}",
    )

async def send_to_wa(phone, m: types.Message, sphere='biz'):
    chat_id = f"{phone}@c.us"
    u_send, u_file = _wa_urls(sphere)
    if m.text:
        await _wa_post(u_send, {"chatId": chat_id, "message": m.text})
    elif m.photo:
        f = await bot.get_file(m.photo[-1].file_id)
        await _wa_post(u_file, {"chatId": chat_id, "urlFile": f"https://api.telegram.org/file/bot{API_TOKEN}/{f.file_path}", "fileName": "img.jpg"})
    elif m.voice:
        f = await bot.get_file(m.voice.file_id)
        await _wa_post(u_file, {"chatId": chat_id, "urlFile": f"https://api.telegram.org/file/bot{API_TOKEN}/{f.file_path}", "fileName": "audio.ogg"})
    elif m.video:
        f = await bot.get_file(m.video.file_id)
        await _wa_post(u_file, {"chatId": chat_id, "urlFile": f"https://api.telegram.org/file/bot{API_TOKEN}/{f.file_path}", "fileName": "video.mp4"})
    elif m.document:
        f = await bot.get_file(m.document.file_id)
        await _wa_post(u_file, {"chatId": chat_id, "urlFile": f"https://api.telegram.org/file/bot{API_TOKEN}/{f.file_path}", "fileName": m.document.file_name or "file"})

# --- МЕНЮ ---
def get_main_menu(uid):
    u = execute_query("SELECT role, sphere, COALESCE(can_receive_leads, 0) FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not u: return types.ReplyKeyboardRemove()
    role, sphere, can_leads = u[0], u[1], u[2]
    kb = ReplyKeyboardBuilder()
    if role == 'owner':
        kb.button(text="🏥 МЕДИЦИНА"); kb.button(text="💼 БИЗНЕС")
        kb.adjust(2)
        return kb.as_markup(resize_keyboard=True)
    if role == 'admin' and sphere == 'med':
        kb.button(text="📊 Нагруженность"); kb.button(text="📅 Записать к Ганчине")
        kb.button(text="💬 Начать диалог"); kb.button(text="🔍 Поиск лида"); kb.button(text="📌 Доработать"); kb.button(text="💰 Оплаты"); kb.button(text="🔄 Продлить курс")
        kb.button(text="◀ Назад в меню")
        kb.adjust(2)
        return kb.as_markup(resize_keyboard=True)
    if role == 'manager' and sphere == 'med':
        kb.button(text="📋 Мои записи"); kb.button(text="💰 Мои оплаты"); kb.button(text="⏳ Дожим"); kb.button(text="📌 Доработать")
        kb.adjust(2)
        return kb.as_markup(resize_keyboard=True)
    # Бизнес: admin или manager
    l_on = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
    l_btn = "🟢 ВКЛ ЛИДЫ" if (l_on and l_on[0] == '1') else "🔴 ВЫКЛ ЛИДЫ"
    if role == 'admin' and (sphere == 'biz' or sphere is None):
        personal = "📥 Мои лиды: ВКЛ" if can_leads else "📥 Мои лиды: ВЫКЛ"
        kb.button(text="📈 Статистика"); kb.button(text="👤 KPI Менеджеров")
        kb.button(text="📋 Лиды в работе"); kb.button(text="💬 Начать диалог")
        kb.button(text="🔍 Поиск лида"); kb.button(text="📌 Доработать"); kb.button(text="📥 Поступления лидов")
        kb.button(text=l_btn); kb.button(text=personal)
    elif role == 'manager' and (sphere == 'biz' or sphere is None):
        kb.button(text="⏳ Дожим"); kb.button(text="✅ Оплачено"); kb.button(text="❌ Отказ"); kb.button(text="📌 Доработать")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def get_owner_med_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📊 Нагруженность"); kb.button(text="📈 Статистика лидов")
    kb.button(text="👤 Назначить Админа"); kb.button(text="📂 Загрузка данных")
    kb.button(text="👑 Дать права владельца"); kb.button(text="💰 Приход"); kb.button(text="🎯 План/KPI"); kb.button(text="🎯 Поставить План")
    kb.button(text="📋 Лиды в работе"); kb.button(text="📥 Поступления лидов (мед)"); kb.button(text="💬 Начать диалог")
    kb.button(text="🔍 Поиск лида"); kb.button(text="📂 Выгрузка (мед)"); kb.button(text="📌 Доработать"); kb.button(text="🔥 Уволить"); kb.button(text="◀ Назад")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def get_owner_biz_menu():
    l_on = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
    l_btn = "🟢 ВКЛ ЛИДЫ" if (l_on and l_on[0] == '1') else "🔴 ВЫКЛ ЛИДЫ"
    kb = ReplyKeyboardBuilder()
    kb.button(text="📈 Статистика"); kb.button(text="👤 KPI Менеджеров")
    kb.button(text="📋 Лиды в работе"); kb.button(text="💬 Начать диалог")
    kb.button(text="🔍 Поиск лида"); kb.button(text="📥 Поступления лидов")
    kb.button(text=l_btn)
    kb.button(text="📌 Доработать"); kb.button(text="👤 Назначить админа бизнеса"); kb.button(text="👑 Дать права владельца")
    kb.button(text="📂 Загрузка данных"); kb.button(text="🔥 Уволить"); kb.button(text="🎯 Поставить План")
    kb.button(text="◀ Назад")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def get_lead_direction(phone):
    row = execute_query("SELECT direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
    return (row[0] or 'biz') if row else 'biz'

def log_lead_event(phone: str, event_type: str, event_data: str = "", user_id: int = None):
    """event_type: incoming, outgoing, status_change, manager_change"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    execute_query(
        "INSERT INTO lead_events (phone, event_type, event_data, created_at, user_id) VALUES (?, ?, ?, ?, ?)",
        (phone, event_type, (event_data or "")[:500], now, user_id),
    )

def chat_history_add(user_id: int, message_id: int, phone: str = None, context: str = None):
    """Сохранить message_id сервисного сообщения бота для последующей очистки."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        "INSERT INTO chat_history (user_id, message_id, phone, created_at, context) VALUES (?, ?, ?, ?, ?)",
        (user_id, message_id, phone or "", now, context or ""),
    )

async def chat_history_delete_messages(user_id: int, phone: str = None, context: str = None):
    """Удалить в Telegram все сообщения из chat_history по user_id и опционально phone/context, затем удалить записи из БД."""
    if phone is not None:
        cond = "user_id = ? AND phone = ?"
        params = (user_id, phone)
    elif context:
        cond = "user_id = ? AND context = ?"
        params = (user_id, context)
    else:
        cond = "user_id = ?"
        params = (user_id,)
    rows = execute_query(f"SELECT message_id FROM chat_history WHERE {cond}", params, fetchall=True)
    for (mid,) in (rows or []):
        try:
            await bot.delete_message(chat_id=user_id, message_id=mid)
        except Exception:
            pass
    execute_query(f"DELETE FROM chat_history WHERE {cond}", params)

def get_managers_by_direction(direction: str):
    """Список (user_id, fio) менеджеров/админов по направлению (biz | med)."""
    if direction == 'med':
        rows = execute_query(
            "SELECT user_id, fio FROM users WHERE (role='manager' OR (role='admin' AND COALESCE(can_receive_leads,0)=1)) AND sphere='med' AND fio IS NOT NULL",
            fetchall=True,
        )
    else:
        rows = execute_query(
            "SELECT user_id, fio FROM users WHERE (role='manager' OR (role='admin' AND COALESCE(can_receive_leads,0)=1)) AND (sphere='biz' OR sphere IS NULL) AND fio IS NOT NULL",
            fetchall=True,
        )
    return rows or []

def build_lead_card(phone: str, can_reassign: bool = False) -> tuple:
    """Возвращает (текст карточки, InlineKeyboardMarkup). can_reassign — показывать кнопку Переназначить."""
    row = execute_query(
        "SELECT name, status, manager_id, touches, last_touch, direction FROM leads WHERE phone = ?",
        (phone,),
        fetchone=True,
    )
    if not row:
        return "Лид не найден.", None
    name, status, mgr_id, touches, last_touch, direction = row[0], row[1], row[2], row[3], row[4], (row[5] or "biz")
    mgr_fio = "—"
    if mgr_id:
        u = execute_query("SELECT fio FROM users WHERE user_id = ?", (mgr_id,), fetchone=True)
        if u:
            mgr_fio = u[0] or str(mgr_id)
    lt = (last_touch[:16] if last_touch and len(last_touch) >= 16 else last_touch) if last_touch else "—"
    direction_label = "Медицина" if direction == "med" else "Бизнес"
    text = (
        f"👤 <b>{name or phone}</b>\n"
        f"📞 {phone}\n"
        f"📂 {direction_label} · Статус: {status or '—'}\n"
        f"👔 Менеджер: {mgr_fio}\n"
        f"📊 Касания: {touches or 0} · Последний контакт: {lt}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать", callback_data=f"wlead_{phone[:30]}")
    kb.button(text="📞 Позвонить", callback_data=f"call_{phone[:30]}")
    kb.button(text="📜 История", callback_data=f"hist_{phone[:30]}")
    if can_reassign:
        kb.button(text="🔄 Переназначить", callback_data=f"re_{phone[:30]}")
    kb.adjust(2)
    return text, kb.as_markup()

def get_med_finish_dialog_kb():
    """Клавиатура для менеджера медицины в режиме диалога с пациентом."""
    kb = ReplyKeyboardBuilder()
    kb.button(text="✅ Завершить диалог")
    return kb.as_markup(resize_keyboard=True)

def is_sunday(d: datetime.date):
    return d.weekday() == 6  # 0=Mon, 6=Sun

def appointment_count(date_str, doctor):
    row = execute_query("SELECT COUNT(*) FROM appointments WHERE date = ? AND doctor = ?", (date_str, doctor), fetchone=True)
    return row[0] if row else 0

def can_add_appointment(date_str, doctor):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False, "Неверный формат даты (нужно ГГГГ-ММ-ДД)."
    if is_sunday(dt):
        return False, "Воскресенье — выходной (режим 6/1)."
    limit = DOCTOR_LIMITS.get(doctor, 0)
    cnt = appointment_count(date_str, doctor)
    if cnt >= limit:
        return False, f"Лимит записей на этот день исчерпан ({cnt}/{limit})."
    return True, None

def _extract_wa_text(body):
    if body.get('messageData', {}).get('textMessageData'):
        return body['messageData']['textMessageData'].get('textMessage', '...')
    if body.get('messageData', {}).get('fileMessageData'):
        return '[Файл/голос]'
    return '...'

def _get_wa_file_info(body):
    """Если входящее сообщение — файл (фото/видео/аудио/документ), возвращает (typeMessage, downloadUrl, caption). Иначе None."""
    md = body.get('messageData') or {}
    t = md.get('typeMessage')
    if t not in ('imageMessage', 'videoMessage', 'audioMessage', 'documentMessage'):
        return None
    fmd = md.get('fileMessageData') or {}
    url = fmd.get('downloadUrl')
    if not url:
        return None
    caption = (fmd.get('caption') or '').strip() or None
    return (t, url, caption)

async def _send_wa_text(api_url, token, chat_id, text):
    url = f"{api_url}/sendMessage/{token}"
    await _wa_post(url, {"chatId": chat_id, "message": text})

# Текст напоминания тем, кому не ответили больше 24 ч (таджикский)
REMINDER_24H_TEXT = "Салом, узр хохиш зиёд барои шуморо бе чавоб мондан, хохиш мекунем якбори дигар саволатонро такрор кунед!"
REMINDER_24H_HOURS = 24
REMINDER_24H_WINDOW_HOURS = 48  # следующий ответ в течение 48ч после напоминания = лид на доработку
DOCTOR_LIMITS = {"Ганчина": 10}  # лимиты записей к врачам на день
CHATTING_IDLE_MINUTES = 20  # после этого бот напоминает: «Завершите диалог, если закончили»

# --- МОНИТОРИНГ: два инстанса параллельно ---
async def _process_wa_instance(instance_name, base_url, instance_id, token):
    """Обработка одного инстанса WA. instance_name = 'biz' | 'med'."""
    receive_url = f"{base_url}/waInstance{instance_id}/receiveNotification/{token}"
    delete_url = f"{base_url}/waInstance{instance_id}/deleteNotification/{token}"
    send_msg_url = f"{base_url}/waInstance{instance_id}/sendMessage/{token}"
    chat_prefix = ""
    med_log_interval = 0  # счётчик для периодического лога "MED: polling"
    while True:
        try:
            processed_this_cycle = 0
            max_per_cycle = 20  # не обрабатывать подряд больше N уведомлений — дать боту обработать Telegram
            while True:
                status, text, j = await _wa_get(receive_url)
                if instance_name == 'med':
                    med_log_interval += 1
                    if status != 200:
                        try:
                            err_body = text[:500] if text else ""
                        except Exception:
                            err_body = ""
                        logging.warning("MED: receiveNotification вернул HTTP %s — %s", status, err_body)
                        print(f"[CRM] MED: HTTP {status} — {err_body}")
                        break
                    try:
                        pass  # j уже распарсен в _wa_get
                    except Exception as e:
                        logging.warning("MED: ответ не JSON: %s", e)
                        print(f"[CRM] MED: ответ не JSON — {e}")
                        break
                    if not j:
                        if med_log_interval % 30 == 1:
                            print(f"[CRM] MED: опрос пустой (инстанс {instance_id}), ждём сообщений...")
                        break
                else:
                    if status != 200 or not j:
                        break
                d = j
                rid = d.get('receiptId')
                body = d.get('body', {})
                tw = body.get('typeWebhook')
                if tw not in ('incomingMessageReceived', 'incomingFileMessageReceived'):
                    if instance_name == 'med':
                        logging.info("MED: тип события %s (пропускаем)", tw)
                        print(f"[CRM] MED: typeWebhook={tw!r} — пропуск")
                    if rid:
                        await _wa_delete(f"{base_url}/waInstance{instance_id}/deleteNotification/{token}/{rid}")
                    continue
                raw_phone = body.get('senderData', {}).get('chatId', '').split('@')[0]
                phone = ''.join(c for c in raw_phone if c.isdigit()) or raw_phone
                if not phone:
                    if rid:
                        await _wa_delete(f"{base_url}/waInstance{instance_id}/deleteNotification/{token}/{rid}")
                    continue
                if instance_name == 'med':
                    logging.info("MED: получено сообщение от %s (typeWebhook=%s)", phone, tw)
                    print(f"[CRM] MED: получено сообщение от {phone}")
                chat_id = f"{phone}@c.us"
                exist = execute_query("SELECT manager_id, name, status, direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
                if exist:
                    mgr_id, c_name, status = exist[0], exist[1], exist[2]
                    lead_dir = (exist[3] or 'biz') if len(exist) > 3 else 'biz'
                    if lead_dir != instance_name:
                        if instance_name == 'med':
                            logging.info("MED: сообщение от %s проигнорировано (лид в базе как direction=%s)", phone, lead_dir)
                            print(f"[CRM] MED: сообщение от {phone} проигнорировано (лид уже в базе как {lead_dir})")
                        await _wa_delete(delete_url + "/" + str(rid))
                        continue
                    execute_query("UPDATE leads SET last_touch = ? WHERE phone = ?", (datetime.now().strftime("%Y-%m-%d %H:%M"), phone))
                    is_active = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (mgr_id,), fetchone=True)
                    if not is_active or (instance_name == 'med' and is_active[1] != 'med'):
                        # Лиды НЕ идут владельцу: ставим в очередь (pending), сообщение никому не пересылаем.
                        execute_query("UPDATE leads SET manager_id = NULL, status = 'pending' WHERE phone = ?", (phone,))
                        await _wa_delete(delete_url + "/" + str(rid))
                        processed_this_cycle += 1
                        continue
                    # Режим «прямого коридора»: лид в статусе chatting — пересылаем только текстом (без кнопок), сохраняем message_id для чистки.
                    if status == 'chatting':
                        txt = _extract_wa_text(body) if not body.get('messageData', {}).get('fileMessageData') else '[Файл/голос]'
                        log_lead_event(phone, "incoming", txt[:200])
                        file_info = _get_wa_file_info(body)
                        sent = None
                        if file_info:
                            ftype, url, fcap = file_info
                            cap = f"💬 {c_name} ({phone}): {fcap}" if fcap else f"💬 {c_name} ({phone})"
                            try:
                                if ftype == "imageMessage":
                                    sent = await bot.send_photo(mgr_id, photo=url, caption=cap)
                                elif ftype == "videoMessage":
                                    sent = await bot.send_video(mgr_id, video=url, caption=cap)
                                elif ftype == "audioMessage":
                                    sent = await bot.send_voice(mgr_id, voice=url, caption=cap)
                                elif ftype == "documentMessage":
                                    sent = await bot.send_document(mgr_id, document=url, caption=cap)
                                else:
                                    sent = await bot.send_message(mgr_id, f"💬 {c_name} ({phone}): [файл]")
                            except Exception:
                                sent = await bot.send_message(mgr_id, f"💬 {c_name} ({phone}): {txt or '[файл]'}")
                        else:
                            sent = await bot.send_message(mgr_id, f"💬 {c_name} ({phone}):\n{txt}")
                        if sent:
                            chat_history_add(mgr_id, sent.message_id, phone=phone, context="session")
                        await _wa_delete(delete_url + "/" + str(rid))
                        processed_this_cycle += 1
                        await asyncio.sleep(0)
                        continue
                    else:
                        kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone}").adjust(2).as_markup()
                    txt = _extract_wa_text(body) if not body.get('messageData', {}).get('fileMessageData') else '[Файл/голос]'
                    is_follow_up = False
                    rem = execute_query("SELECT reminded_at FROM reminded_24h WHERE phone = ? AND sphere = ?", (phone, instance_name), fetchone=True)
                    if rem:
                        try:
                            reminded = datetime.strptime(rem[0][:16], "%Y-%m-%d %H:%M")
                            if (datetime.now() - reminded).total_seconds() <= REMINDER_24H_WINDOW_HOURS * 3600:
                                is_follow_up = True
                                execute_query("INSERT OR REPLACE INTO follow_up_queue (phone, direction, created_at, last_message) VALUES (?, ?, ?, ?)",
                                    (phone, instance_name, datetime.now().strftime("%Y-%m-%d %H:%M"), txt[:200]))
                                execute_query("DELETE FROM reminded_24h WHERE phone = ? AND sphere = ?", (phone, instance_name))
                        except Exception:
                            pass
                    file_info = _get_wa_file_info(body)
                    cap_prefix = f"📌 Доработка (не отвечали 24ч): {c_name} ({phone})" if is_follow_up else f"💬 {c_name} ({phone})"
                    if file_info:
                        ftype, url, fcap = file_info
                        log_lead_event(phone, "incoming", f"[{ftype}]" + (fcap or "")[:180])
                        caption = f"{cap_prefix}\n{fcap}" if fcap else cap_prefix
                        try:
                            if ftype == "imageMessage":
                                await bot.send_photo(mgr_id, photo=url, caption=caption, reply_markup=kb)
                            elif ftype == "videoMessage":
                                await bot.send_video(mgr_id, video=url, caption=caption, reply_markup=kb)
                            elif ftype == "audioMessage":
                                await bot.send_voice(mgr_id, voice=url, caption=caption, reply_markup=kb)
                            elif ftype == "documentMessage":
                                await bot.send_document(mgr_id, document=url, caption=caption, reply_markup=kb)
                            else:
                                await bot.send_message(mgr_id, f"💬 <b>{c_name}</b> ({phone}):\n[файл]", reply_markup=kb, parse_mode="HTML")
                        except Exception as e:
                            logging.warning("WA file forward failed %s: %s", ftype, e)
                            await bot.send_message(mgr_id, f"💬 <b>{c_name}</b> ({phone}):\n[файл: {ftype}] (не удалось переслать)", reply_markup=kb, parse_mode="HTML")
                    else:
                        txt = _extract_wa_text(body)
                        log_lead_event(phone, "incoming", txt[:200])
                        await bot.send_message(mgr_id, f"💬 <b>{c_name}</b> ({phone}):\n{txt}", reply_markup=kb, parse_mode="HTML")
                else:
                    q = execute_query("SELECT step, instance_sphere FROM auth_queue WHERE phone = ?", (phone,), fetchone=True)
                    if not q:
                        if instance_name == 'med':
                            logging.info("MED: первый контакт с %s, просим имя", phone)
                            print(f"[CRM] MED: первый контакт с {phone}, просим имя")
                        msg = "Салом, ном ва насаби худро нависед! Мо дар муддати кутоҳтарин ба шумо ҷавоб медиҳем!"
                        await _wa_post(send_msg_url, {"chatId": chat_id, "message": msg})
                        execute_query("INSERT OR REPLACE INTO auth_queue (phone, step, instance_sphere) VALUES (?, 1, ?)", (phone, instance_name))
                    elif q[0] == 1 and (q[1] or 'biz') == instance_name:
                        if instance_name == 'med':
                            logging.info("MED: второе сообщение (имя) от %s, создаём лид", phone)
                            print(f"[CRM] MED: второе сообщение (имя) от {phone}, создаём лид")
                        c_name = _extract_wa_text(body)
                        if c_name == '[Файл/голос]':
                            c_name = 'Клиент'
                        execute_query("DELETE FROM auth_queue WHERE phone = ?", (phone,))
                        # Один лид в одни руки: только свободный менеджер (is_busy=0 и нет active/chatting лидов).
                        target = get_free_manager_for_direction(instance_name)
                        if target:
                            execute_query("UPDATE users SET is_busy=1 WHERE user_id=?", (target,))
                            execute_query(
                                "INSERT INTO leads (phone, name, status, manager_id, last_touch, touches, direction) VALUES (?, ?, 'active', ?, ?, 1, ?)",
                                (phone, c_name, target, datetime.now(), instance_name),
                            )
                            kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone}").adjust(2).as_markup()
                            logging.info("%s: new lead %s -> manager %s", instance_name, phone, target)
                            print(f"[CRM] {instance_name}: new lead {phone} -> manager {target}")
                            try:
                                await bot.send_message(target, f"📥 <b>НОВЫЙ ЛИД</b>\n👤 {c_name}\n📞 {phone}", reply_markup=kb, parse_mode="HTML")
                            except Exception as e:
                                logging.exception("send_message to manager %s failed: %s", target, e)
                                print(f"[CRM] Ошибка отправки карточки менеджеру {target}: {e}")
                        else:
                            # Все заняты — лид только в очереди. Владельцу не шлём — лид придёт освободившемуся менеджеру.
                            execute_query(
                                "INSERT INTO leads (phone, name, status, manager_id, last_touch, touches, direction) VALUES (?, ?, 'pending', NULL, ?, 1, ?)",
                                (phone, c_name, datetime.now(), instance_name),
                            )
                            logging.warning("%s: all busy, lead %s in queue (pending)", instance_name, phone)
                            print(f"[CRM] {instance_name}: все заняты, лид {phone} в очереди")
                            try:
                                await asyncio.sleep(0.3)
                            except Exception:
                                pass
                await _wa_delete(delete_url + "/" + str(rid))
                processed_this_cycle += 1
                await asyncio.sleep(0)  # отдать event loop — чтобы бот не «зависал»
                if processed_this_cycle >= max_per_cycle:
                    break
        except asyncio.TimeoutError:
            logging.warning("check_wa %s: Green API read timeout, retrying.", instance_name)
        except (aiohttp.ClientError, aiohttp.ClientConnectorError) as e:
            logging.warning("check_wa %s: request error %s", instance_name, e)
        except Exception as e:
            logging.exception("check_wa %s: %s", instance_name, e)
        await asyncio.sleep(1)

async def check_wa_biz():
    await _process_wa_instance('biz', API_URL, ID_INSTANCE, API_TOKEN_INSTANCE)

async def check_wa_med():
    await _process_wa_instance('med', MED_API_URL, MED_ID_INSTANCE, MED_API_TOKEN)

async def _wa_send_reminder(instance_name: str):
    """Отправить напоминание (таджикский текст) в WA тем, кому не отвечали 24+ ч."""
    if instance_name == 'med':
        base_url, iid, token = MED_API_URL, MED_ID_INSTANCE, MED_API_TOKEN
        cond = "direction = 'med'"
    else:
        base_url, iid, token = API_URL, ID_INSTANCE, API_TOKEN_INSTANCE
        cond = "(direction = 'biz' OR direction IS NULL)"
    send_url = f"{base_url}/waInstance{iid}/sendMessage/{token}"
    already = set(r[0] for r in execute_query("SELECT phone FROM reminded_24h WHERE sphere = ?", (instance_name,), fetchall=True))
    rows = execute_query(f"SELECT phone FROM leads WHERE {cond}", fetchall=True)
    cutoff = datetime.now() - timedelta(hours=REMINDER_24H_HOURS)
    for (phone,) in rows:
        if phone in already:
            continue
        last_ev = execute_query("SELECT event_type, created_at FROM lead_events WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,), fetchone=True)
        if not last_ev or last_ev[0] != 'incoming':
            continue
        try:
            ev_time = datetime.strptime(last_ev[1][:16], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if ev_time > cutoff:
            continue
        chat_id = f"{phone}@c.us"
        try:
            await _wa_post(send_url, {"chatId": chat_id, "message": REMINDER_24H_TEXT})
        except Exception as e:
            logging.warning("Reminder send failed %s: %s", phone, e)
            continue
        execute_query("INSERT OR REPLACE INTO reminded_24h (phone, sphere, reminded_at) VALUES (?, ?, ?)",
            (phone, instance_name, datetime.now().strftime("%Y-%m-%d %H:%M")))
        already.add(phone)

async def job_remind_24h():
    """Периодически проверять лиды без ответа 24ч и отправлять напоминание в WA."""
    while True:
        try:
            await _wa_send_reminder('biz')
            await _wa_send_reminder('med')
        except Exception as e:
            logging.exception("job_remind_24h: %s", e)
        await asyncio.sleep(3600)

async def job_chatting_idle():
    """Если менеджер открыл коридор (chatting), но не пишет клиенту > 20 мин — напомнить."""
    while True:
        try:
            await asyncio.sleep(60)
            cutoff = (datetime.now() - timedelta(minutes=CHATTING_IDLE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            rows = execute_query(
                "SELECT user_id, phone, reminder_sent FROM chat_sessions WHERE last_outgoing_at < ? AND COALESCE(reminder_sent, 0) = 0",
                (cutoff,),
                fetchall=True,
            )
            for (uid, phone, _) in (rows or []):
                try:
                    await bot.send_message(uid, "⚠️ Вы не отвечаете клиенту! Завершите диалог, если закончили.")
                    execute_query("UPDATE chat_sessions SET reminder_sent = 1 WHERE user_id = ? AND phone = ?", (uid, phone))
                except Exception as e:
                    logging.warning("chatting_idle reminder to %s: %s", uid, e)
        except Exception as e:
            logging.exception("job_chatting_idle: %s", e)

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    init_db(); u = execute_query("SELECT role FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        await state.set_state(Form.reg_fio); await m.answer("🏢 <b>METODI CRM</b>\nВведите ваше полное <b>ФИО</b>:")
    else:
        await m.answer("🏢 Работаем!", reply_markup=get_main_menu(m.from_user.id))

@dp.message(Command("reset_user"))
async def cmd_reset_user(m: types.Message):
    """Владелец: сбросить пользователя из базы — он сможет заново подать заявку. /reset_user <Telegram ID>"""
    if not is_owner(m.from_user.id):
        return
    parts = (m.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /reset_user <Telegram ID>\nПример: /reset_user 123456789\nПользователь будет удалён из базы и сможет заново нажать /start и подать заявку.")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await m.answer("ID должен быть числом. Пример: /reset_user 123456789")
        return
    if uid == m.from_user.id:
        await m.answer("Нельзя сбросить себя.")
        return
    execute_query("DELETE FROM users WHERE user_id = ?", (uid,))
    execute_query("DELETE FROM pending_reg WHERE user_id = ?", (uid,))
    await m.answer(f"✅ Пользователь {uid} удалён из базы. Он может заново написать боту /start и подать заявку.")

@dp.callback_query(F.data.startswith("cl_") | F.data.startswith("f_"))
async def closing(c: types.CallbackQuery, state: FSMContext):
    p = c.data.split("_")[-1]
    if "cl_" in c.data:
        direction = get_lead_direction(p)
        if direction == 'med':
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ Отказ", callback_data=f"med_r_{p}")
            kb.button(text="⏳ Подумает", callback_data=f"med_t_{p}")
            kb.button(text="💰 Оплатил", callback_data=f"med_p_{p}")
            kb.button(text="🚫 НЕ ОТВЕЧАЮТ", callback_data=f"med_n_{p}")
            kb.adjust(1)
            await c.message.answer("Итог звонка:", reply_markup=kb.as_markup())
        else:
            kb = InlineKeyboardBuilder()
            kb.button(text="💰 ОПЛАТИЛ", callback_data=f"f_s_{p}")
            kb.button(text="⏳ ДУМАЕТ", callback_data=f"f_t_{p}")
            kb.button(text="❌ ОТКАЗ", callback_data=f"f_r_{p}")
            kb.button(text="🚫 НЕ ОТВЕТИЛ", callback_data=f"f_n_{p}")
            await c.message.answer("Итог звонка:", reply_markup=kb.adjust(1).as_markup())
    elif "f_" in c.data:
        res = c.data.split("_")[1]
        if res == 'n':
            l_data = execute_query("SELECT touches, name FROM leads WHERE phone = ?", (p,), fetchone=True)
            new_t = l_data[0] + 1
            if new_t >= 7:
                execute_query("UPDATE leads SET status='closed', touches=7, comment='Автозакрытие: 7 касаний' WHERE phone=?", (p,))
                execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
                await c.message.answer(f"🛑 Лид {l_data[1]} закрыт (7 касаний).")
            else:
                execute_query("UPDATE leads SET touches=?, last_touch=? WHERE phone=?", (new_t, datetime.now(), p))
                await c.message.answer(f"🔄 Касание №{new_t} зафиксировано.")
        else:
            await state.update_data(c_phone=p, c_status=res)
            await state.set_state(Form.closing_sphere); await c.message.answer("Сфера:")
    await c.answer()

# --- Владелец: выбор направления и Назад ---
@dp.message(F.text == "🏥 МЕДИЦИНА")
async def owner_med(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if u and u[0] == 'owner':
        await state.update_data(owner_current_sphere='med')
        await m.answer("🏥 Меню «Медицина»", reply_markup=get_owner_med_menu())

@dp.message(F.text == "💼 БИЗНЕС")
async def owner_biz(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if u and u[0] == 'owner':
        await state.update_data(owner_current_sphere='biz')
        await m.answer("💼 Меню «Бизнес»", reply_markup=get_owner_biz_menu())

@dp.message(F.text == "◀ Назад")
@dp.message(F.text == "◀ Назад в меню")
async def back_main(m: types.Message):
    await m.answer("Главное меню", reply_markup=get_main_menu(m.from_user.id))

# --- Бизнес: статистика (только для владельца/админа в контексте бизнеса) ---
def _biz_leads_cond():
    return " (direction = 'biz' OR direction IS NULL) "

@dp.message(F.text == "📈 Статистика")
async def stats(m: types.Message):
    await chat_history_delete_messages(m.from_user.id, context="kpi")
    cond = _biz_leads_cond()
    all_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE 1=1 AND {cond.strip()}", fetchone=True)[0] or 1
    ans_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE is_answered=1 AND {cond.strip()}", fetchone=True)[0]
    sold_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE status='closed' AND comment NOT LIKE '%Автозакрытие%' AND is_answered=1 AND {cond.strip()}", fetchone=True)[0]
    c_ans = round((ans_l / all_l) * 100, 1)
    c_sale = round((sold_l / (ans_l or 1)) * 100, 1)
    msg = await m.answer(f"📊 <b>ОБЩАЯ ВОРОНКА (Бизнес)</b>\n\n📥 Лидов: {all_l}\n📞 Дозвоны: {ans_l} ({c_ans}%)\n💰 Продажи: {sold_l} ({c_sale}% из дозвонов)", parse_mode="HTML")
    chat_history_add(m.from_user.id, msg.message_id, context="stats")

@dp.message(F.text == "👤 KPI Менеджеров")
async def mgr_kpi(m: types.Message):
    await chat_history_delete_messages(m.from_user.id, context="stats")
    st = execute_query("SELECT user_id, fio, plan, sphere FROM users WHERE role='manager'", fetchall=True)
    txt = "👤 <b>KPI МЕНЕДЖЕРОВ (Бизнес)</b>\n\n"
    for row in st:
        mid, fio, plan = row[0], row[1], row[2]
        sphere = row[3] if len(row) > 3 else None
        if sphere == 'med':
            continue
        m_all = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND (direction = 'biz' OR direction IS NULL)", (mid,), fetchone=True)[0] or 1
        m_ans = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND is_answered=1 AND (direction = 'biz' OR direction IS NULL)", (mid,), fetchone=True)[0]
        m_sold = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND status='closed' AND is_answered=1 AND comment NOT LIKE '%Автозакрытие%' AND (direction = 'biz' OR direction IS NULL)", (mid,), fetchone=True)[0]
        m_t = execute_query("SELECT AVG(touches) FROM leads WHERE manager_id=? AND (direction = 'biz' OR direction IS NULL)", (mid,), fetchone=True)[0] or 0
        perc = round((m_sold / (plan or 1)) * 100, 1)
        conv = round((m_ans / m_all) * 100, 1)
        txt += f"▪️ <b>{fio}</b>\n   План: {perc}% ({m_sold}/{plan})\n   Дозвон: {conv}%\n   Ср. касаний: {round(m_t, 1)}\n\n"
    msg = await m.answer(txt or "Нет менеджеров бизнеса.", parse_mode="HTML")
    chat_history_add(m.from_user.id, msg.message_id, context="kpi")

@dp.message(F.text == "📥 Поступления лидов")
async def biz_leads_flow_menu(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    if not ((role == 'owner') or (role == 'admin' and (sphere == 'biz' or sphere is None))):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="День", callback_data="bizlead_day")
    kb.button(text="Неделя", callback_data="bizlead_week")
    kb.button(text="Месяц", callback_data="bizlead_month")
    kb.button(text="Другая дата/период", callback_data="bizlead_custom")
    await m.answer("Поступления лидов (Бизнес). Период:", reply_markup=kb.adjust(2).as_markup())

@dp.callback_query(F.data.startswith("bizlead_"))
async def biz_leads_flow_cb(c: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(c)  # сразу, чтобы не было "query is too old"
    period = c.data.replace("bizlead_", "")
    if period == "custom":
        await state.set_state(Form.biz_leads_custom_period)
        await c.message.answer(
            "Введите дату или период:\n"
            "- один день: 2026-02-24\n"
            "- период: 2026-02-01 2026-02-24"
        )
        return
    now = datetime.now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)
    end = now
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")
    cond = " (direction = 'biz' OR direction IS NULL) "
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND is_answered = 1 AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    conv = round((processed / (total or 1)) * 100, 1)
    await c.message.edit_text(
        f"📥 <b>Поступления лидов (Бизнес)</b> — {period}\n\n"
        f"Пришло лидов: {total}\n"
        f"Обработано: {processed} ({conv}%)",
        parse_mode="HTML",
    )

@dp.message(Form.biz_leads_custom_period)
async def biz_leads_flow_custom_period(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    if not ((role == 'owner') or (role == 'admin' and (sphere == 'biz' or sphere is None))):
        return
    parts = m.text.strip().split()
    try:
        if len(parts) == 1:
            d1 = datetime.strptime(parts[0], "%Y-%m-%d").date()
            d2 = d1
        elif len(parts) == 2:
            d1 = datetime.strptime(parts[0], "%Y-%m-%d").date()
            d2 = datetime.strptime(parts[1], "%Y-%m-%d").date()
        else:
            raise ValueError()
    except ValueError:
        await m.answer("Неверный формат. Пример:\n2026-02-24\nили\n2026-02-01 2026-02-24")
        return
    if d2 < d1:
        d1, d2 = d2, d1
    start_dt = datetime.combine(d1, datetime.min.time())
    end_dt = datetime.combine(d2, datetime.max.time()).replace(microsecond=0)
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    cond = " (direction = 'biz' OR direction IS NULL) "
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND is_answered = 1 AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    conv = round((processed / (total or 1)) * 100, 1)
    label = d1.isoformat() if d1 == d2 else f"{d1.isoformat()}—{d2.isoformat()}"
    await m.answer(
        f"📥 <b>Поступления лидов (Бизнес)</b> — {label}\n\n"
        f"Пришло лидов: {total}\n"
        f"Обработано: {processed} ({conv}%)",
        parse_mode="HTML",
    )
    await state.clear()

@dp.message(F.text == "📌 Доработать")
async def follow_up_list(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    if role == 'owner':
        data = await state.get_data()
        sphere = data.get('owner_current_sphere')
        if not sphere:
            await m.answer("Откройте меню 🏥 Медицина или 💼 Бизнес и нажмите «Доработать» снова.")
            return
    elif role not in ('admin', 'manager'):
        return
    if sphere not in ('med', 'biz'):
        sphere = 'biz'
    rows = execute_query("SELECT phone, last_message, created_at FROM follow_up_queue WHERE direction = ? ORDER BY created_at ASC", (sphere,), fetchall=True)
    if not rows:
        await m.answer("Нет клиентов на доработку (кому не отвечали 24ч и они написали снова).")
        return
    kb = InlineKeyboardBuilder()
    for phone, msg, created in rows:
        name_row = execute_query("SELECT name FROM leads WHERE phone = ?", (phone,), fetchone=True)
        name = (name_row[0] if name_row else phone)
        label = f"{name or phone} — {created[:10] if created else ''}"
        if len(label) > 40:
            label = label[:37] + "..."
        kb.button(text=label, callback_data=f"fu_{phone[:30]}")
    kb.adjust(1)
    await m.answer("📌 Клиенты на доработку (нажмите для карточки и действий):", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("fu_"))
async def follow_up_card(c: types.CallbackQuery):
    phone = c.data[3:].strip()
    if not phone:
        await c.answer()
        return
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not u or u[0] not in ('owner', 'admin', 'manager'):
        await c.answer("Доступ запрещён.")
        return
    text, kbd = build_lead_card(phone, can_reassign=(u[0] in ('owner', 'admin')))
    await c.message.edit_text(text or "Лид не найден.", reply_markup=kbd, parse_mode="HTML")
    await c.answer()

@dp.message(F.text == "🔥 Уволить")
async def fire_list(m: types.Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return
    data = await state.get_data()
    sphere = data.get('owner_current_sphere')  # 'med' или 'biz' в зависимости от открытого меню
    if not sphere:
        await m.answer("Откройте меню 🏥 Медицина или 💼 Бизнес и нажмите «Уволить» снова — список будет по выбранному направлению.")
        return
    # Медицина: менеджеры и админы медицины + все владельцы. Бизнес: менеджеры и админы бизнеса + все владельцы.
    if sphere == 'med':
        st = execute_query(
            """SELECT user_id, fio, role FROM users
               WHERE ( (role IN ('manager','admin') AND sphere = 'med') OR role = 'owner' )
               AND user_id != ?
               ORDER BY CASE role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END, fio""",
            (m.from_user.id,),
            fetchall=True,
        )
        title = "Медицина — выберите, кого уволить:"
    else:
        st = execute_query(
            """SELECT user_id, fio, role FROM users
               WHERE ( (role IN ('manager','admin') AND (sphere = 'biz' OR sphere IS NULL)) OR role = 'owner' )
               AND user_id != ?
               ORDER BY CASE role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END, fio""",
            (m.from_user.id,),
            fetchall=True,
        )
        title = "Бизнес — выберите, кого уволить:"
    if not st:
        await m.answer("В этом направлении никого нет для увольнения.")
        return
    kb = InlineKeyboardBuilder()
    for row in st:
        sid, fio, role = row[0], row[1] or str(row[0]), row[2]
        label = f"❌ {fio} ({'владелец' if role == 'owner' else 'админ' if role == 'admin' else 'менеджер'})"
        kb.button(text=label, callback_data=f"fr_{sid}")
    await m.answer(title, reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("fr_"))
async def f_conf(c: types.CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer()
        return
    uid = c.data.split("_")[1]
    if uid == str(c.from_user.id):
        await c.answer("Нельзя уволить себя.")
        return
    execute_query("DELETE FROM users WHERE user_id = ?", (int(uid),))
    try:
        await bot.send_message(int(uid), "❌ Ваш доступ к CRM аннулирован.", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
        pass
    await c.message.edit_text("🔥 Сотрудник удалён, доступ закрыт.")
    await c.answer()

# --- ОСТАЛЬНОЕ (БЕЗ ИЗМЕНЕНИЙ) ---
@dp.message(Form.reg_fio)
async def reg_fio(m: types.Message, state: FSMContext):
    execute_query("INSERT OR REPLACE INTO pending_reg (user_id, fio) VALUES (?, ?)", (m.from_user.id, m.text))
    kb = InlineKeyboardBuilder()
    kb.button(text="🏥 МЕДИЦИНА", callback_data=f"ap_med_{m.from_user.id}")
    kb.button(text="💼 БИЗНЕС", callback_data=f"ap_biz_{m.from_user.id}")
    kb.button(text="❌ ОТКЛОНИТЬ", callback_data=f"rj_{m.from_user.id}")
    kb.adjust(2)
    msg_text = f"🔔 ЗАЯВКА: {m.text} (ID: {m.from_user.id})"
    sent = False
    for owner_id in get_all_owner_ids():
        try:
            await bot.send_message(owner_id, msg_text, reply_markup=kb.as_markup(), parse_mode="HTML")
            sent = True
        except Exception as e:
            logging.warning("Не удалось отправить заявку владельцу %s: %s", owner_id, e)
    if not sent:
        logging.error("Заявка от %s не доставлена ни одному владельцу", m.from_user.id)
    await m.answer("✅ Заявка отправлена.")
    await state.clear()

@dp.callback_query(F.data.startswith("ap_med_"))
async def ap_med(c: types.CallbackQuery):
    uid = int(c.data.split("_")[-1])
    row = execute_query("SELECT fio FROM pending_reg WHERE user_id = ?", (uid,), fetchone=True)
    if not row:
        await c.answer("Заявка уже обработана."); return
    fio = row[0]
    execute_query("DELETE FROM pending_reg WHERE user_id = ?", (uid,))
    execute_query("INSERT INTO users (user_id, role, fio, sphere) VALUES (?, 'manager', ?, 'med')", (uid, fio))
    await bot.send_message(uid, "🎉 Одобрено (Медицина)! Жми /start")
    await c.message.edit_text(f"✅ {fio} — Медицина.")
    await c.answer()

@dp.callback_query(F.data.startswith("ap_biz_"))
async def ap_biz(c: types.CallbackQuery):
    uid = int(c.data.split("_")[-1])
    row = execute_query("SELECT fio FROM pending_reg WHERE user_id = ?", (uid,), fetchone=True)
    if not row:
        await c.answer("Заявка уже обработана."); return
    fio = row[0]
    execute_query("DELETE FROM pending_reg WHERE user_id = ?", (uid,))
    execute_query("INSERT INTO users (user_id, role, fio, sphere) VALUES (?, 'manager', ?, 'biz')", (uid, fio))
    await bot.send_message(uid, "🎉 Одобрено (Бизнес)! Жми /start")
    await c.message.edit_text(f"✅ {fio} — Бизнес.")
    await c.answer()

@dp.callback_query(F.data.startswith("rj_"))
async def rj(c: types.CallbackQuery):
    uid = int(c.data.split("_")[-1])
    execute_query("DELETE FROM pending_reg WHERE user_id = ?", (uid,))
    try:
        await bot.send_message(uid, "❌ Заявка отклонена.", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
        pass
    await c.message.edit_text("❌ Заявка отклонена.")
    await c.answer()

# --- Удобство и контроль: поиск лида ---
def _can_search_leads(uid: int) -> tuple:
    """(can_search, allowed_sphere). allowed_sphere: None = все, 'biz' = только бизнес, 'med' = только медицина."""
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not u:
        return False, None
    role, sphere = u[0], u[1]
    if role == "owner":
        return True, None
    if role == "admin" and sphere == "biz":
        return True, "biz"
    if role == "admin" and sphere == "med":
        return True, "med"
    return False, None

@dp.message(F.text == "🔍 Поиск лида")
async def lead_search_btn(m: types.Message, state: FSMContext):
    can, _ = _can_search_leads(m.from_user.id)
    if not can:
        return
    await state.set_state(Form.lead_search_query)
    await m.answer("Введите номер телефона или часть ФИО:")

@dp.message(Form.lead_search_query)
async def lead_search_query(m: types.Message, state: FSMContext):
    can, allowed_sphere = _can_search_leads(m.from_user.id)
    if not can:
        await state.clear()
        return
    q = (m.text or "").strip()
    if not q:
        await m.answer("Введите номер или часть имени.")
        return
    digits = "".join(c for c in q if c.isdigit())
    if allowed_sphere == "biz":
        sphere_cond = " AND (direction = 'biz' OR direction IS NULL)"
    elif allowed_sphere == "med":
        sphere_cond = " AND direction = 'med'"
    else:
        sphere_cond = ""
    if digits:
        leads = execute_query(
            f"SELECT phone, name, status FROM leads WHERE (phone LIKE ? OR replace(replace(phone,'+',''),' ','') LIKE ?){sphere_cond} ORDER BY last_touch DESC LIMIT 20",
            (f"%{digits}%", f"%{digits}%"),
            fetchall=True,
        )
    else:
        leads = []
    if not leads and q:
        leads = execute_query(
            f"SELECT phone, name, status FROM leads WHERE name LIKE ?{sphere_cond} ORDER BY last_touch DESC LIMIT 20",
            (f"%{q}%",),
            fetchall=True,
        )
    await state.clear()
    if not leads:
        await m.answer("Ничего не найдено.")
        return
    if len(leads) == 1:
        phone = leads[0][0]
        text, kbd = build_lead_card(phone, can_reassign=True)
        await m.answer(text, reply_markup=kbd, parse_mode="HTML")
        return
    kb = InlineKeyboardBuilder()
    for phone, name, status in leads:
        label = f"{name or phone} — {status or '?'}"
        if len(label) > 35:
            label = label[:32] + "..."
        cb = f"card_{phone}"[:64]
        kb.button(text=label, callback_data=cb)
    kb.adjust(1)
    await m.answer("Выберите лид:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("card_"))
async def lead_card_show(c: types.CallbackQuery):
    phone = c.data[5:]
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    text, kbd = build_lead_card(phone, can_reassign=True)
    await c.message.edit_text(text, reply_markup=kbd, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("wlead_"))
async def lead_card_write(c: types.CallbackQuery, state: FSMContext):
    phone = c.data[6:].strip()
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    await state.update_data(target=phone, new_chat_sphere=get_lead_direction(phone))
    await state.set_state(Form.waiting_for_reply)
    row = execute_query("SELECT name FROM leads WHERE phone = ?", (phone,), fetchone=True)
    name = row[0] if row else phone
    await c.message.answer(
        f"💬 <b>Диалог с {name}</b> ({phone})\n\nОтправляйте текст, голос, фото, видео — всё уйдёт в WA. По окончании нажмите «Завершить диалог».",
        reply_markup=get_med_finish_dialog_kb(),
        parse_mode="HTML",
    )
    events = execute_query("SELECT event_type, event_data, created_at FROM lead_events WHERE phone = ? ORDER BY id DESC LIMIT 8", (phone,), fetchall=True)
    if events:
        lines = []
        for et, data, created in reversed(events):
            lbl = "📩" if et == "incoming" else "📤" if et == "outgoing" else "🔄"
            lines.append(f"{created} {lbl} {data[:60]}..." if data and len(data) > 60 else f"{created} {lbl} {data or ''}")
        await c.message.answer("📜 Последняя переписка:\n\n" + "\n".join(lines))
    await c.answer()

@dp.callback_query(F.data.startswith("call_"))
async def lead_card_call(c: types.CallbackQuery):
    phone = c.data[5:].strip()
    await c.answer()
    await c.message.answer(f"📞 Позвонить: +{phone}")

@dp.callback_query(F.data.startswith("hist_"))
async def lead_card_history(c: types.CallbackQuery):
    phone = c.data[5:].strip()
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    events = execute_query(
        "SELECT event_type, event_data, created_at FROM lead_events WHERE phone = ? ORDER BY id DESC LIMIT 15",
        (phone,),
        fetchall=True,
    )
    if not events:
        await c.answer("История пуста.")
        return
    lines = []
    for et, data, created in events:
        label = {"incoming": "📩 Входящее", "outgoing": "📤 Исходящее", "status_change": "🔄 Статус", "manager_change": "👤 Менеджер"}.get(et, et)
        lines.append(f"{created} · {label}: {data[:80]}" if data else f"{created} · {label}")
    await c.message.answer("📜 История по лиду:\n\n" + "\n".join(lines))
    await c.answer()

@dp.callback_query(F.data.startswith("re_"))
async def lead_reassign_start(c: types.CallbackQuery):
    payload = c.data[3:]
    idx = payload.rfind("_")
    if idx > 0 and payload[idx + 1 :].isdigit():
        phone = payload[:idx]
        new_uid = payload[idx + 1 :]
        row = execute_query("SELECT manager_id, direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
        if row:
            old_mgr, direction = row[0], (row[1] or 'biz')
            new_uid = int(new_uid)
            lead_row = execute_query("SELECT name FROM leads WHERE phone = ?", (phone,), fetchone=True)
            lead_name = (lead_row[0] if lead_row else phone)
            execute_query("UPDATE leads SET manager_id = ? WHERE phone = ?", (new_uid, phone))
            log_lead_event(phone, "manager_change", f"был {old_mgr} → {new_uid}", c.from_user.id)
            if old_mgr and old_mgr != new_uid:
                try:
                    await bot.send_message(old_mgr, f"🔄 Лид <b>{lead_name}</b> ({phone}) переназначен другому менеджеру.", parse_mode="HTML")
                except Exception:
                    pass
            u = execute_query("SELECT fio, is_busy FROM users WHERE user_id = ?", (new_uid,), fetchone=True)
            mgr_name = u[0] if u else str(new_uid)
            busy = u and u[1] == 1
            kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone}").adjust(2).as_markup()
            text = f"🔄 <b>Вам переназначен лид</b>\n👤 {lead_name}\n📞 {phone}"
            if busy:
                text += "\n\n⚠️ У вас уже есть активный лид — по возможности закройте его быстрее, чтобы взять этого."
            await bot.send_message(new_uid, text, reply_markup=kb, parse_mode="HTML")
            await c.message.edit_text(f"✅ Лид переназначен на {mgr_name}. Менеджеру отправлено уведомление.")
        else:
            await c.answer("Лид не найден.")
        await c.answer()
        return
    phone = payload
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    row = execute_query("SELECT direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    direction = row[0] or "biz"
    managers = get_managers_by_direction(direction)
    if not managers:
        await c.answer("Нет менеджеров в этом направлении.")
        return
    kb = InlineKeyboardBuilder()
    for uid, fio in managers:
        cb = f"re_{phone}_{uid}"[:64]
        kb.button(text=f"👤 {fio}", callback_data=cb)
    kb.adjust(1)
    await c.message.edit_text("Выберите нового менеджера:", reply_markup=kb.as_markup())
    await c.answer()

@dp.message(F.text == "💬 Начать диалог")
async def start_dialog_btn(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    # Владелец: по кнопке из медицины — мед, из бизнеса — по контексту; админ — своя сфера
    if role == 'owner':
        # Сфера берётся из меню: МЕДИЦИНА → med, БИЗНЕС → biz (сохраняем при открытии меню).
        data = await state.get_data()
        owner_sphere = data.get('owner_current_sphere')  # 'med' или 'biz'
        await state.set_state(Form.new_chat_phone)
        await state.update_data(new_chat_sphere=owner_sphere, start_dialog=True)
        await m.answer("Введите номер клиента/пациента (без +):")
        return
    if role == 'admin' and sphere == 'med':
        await state.set_state(Form.new_chat_phone)
        await state.update_data(new_chat_sphere='med', start_dialog=True)
        await m.answer("Введите номер пациента (без +):")
        return
    if role == 'admin' and (sphere == 'biz' or sphere is None):
        await state.set_state(Form.new_chat_phone)
        await state.update_data(new_chat_sphere='biz', start_dialog=True)
        await m.answer("Введите номер клиента (без +):")
        return
    if role == 'manager':
        await state.set_state(Form.new_chat_phone)
        await state.update_data(new_chat_sphere=sphere, start_dialog=True)
        await m.answer("Введите номер (без +):")
        return

@dp.message(Form.new_chat_phone)
async def start_dialog_phone(m: types.Message, state: FSMContext):
    phone = (m.text or "").replace("+", "").strip()
    if not phone:
        await m.answer("Введите номер (без +).")
        return
    d = await state.get_data()
    sphere = d.get('new_chat_sphere')
    if sphere is None:
        sphere = get_lead_direction(phone)
    await state.update_data(target=phone, new_chat_sphere=sphere)
    await state.set_state(Form.waiting_for_reply)
    row = execute_query("SELECT name FROM leads WHERE phone = ?", (phone,), fetchone=True)
    name = row[0] if row else phone
    if row:
        execute_query("UPDATE leads SET status = 'chatting' WHERE phone = ?", (phone,))
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("INSERT OR REPLACE INTO chat_sessions (user_id, phone, last_outgoing_at, reminder_sent) VALUES (?, ?, ?, 0)", (m.from_user.id, phone, now_iso))
        await chat_history_delete_messages(m.from_user.id, phone=phone)
    msg = await m.answer(
        f"💬 <b>Диалог с {name}</b> ({phone})\n\nОтправляйте текст, голос, фото — всё уйдёт в WA. Внизу кнопка «Завершить диалог».",
        reply_markup=get_med_finish_dialog_kb(),
        parse_mode="HTML",
    )
    if row:
        chat_history_add(m.from_user.id, msg.message_id, phone=phone, context="session")

@dp.callback_query(F.data.startswith("rp_"))
async def reply_start(c: types.CallbackQuery, state: FSMContext):
    phone = c.data.split("_")[1]
    if len(phone) > 30:
        phone = phone[:30]
    await state.update_data(target=phone)
    sphere = get_lead_direction(phone)
    await state.update_data(new_chat_sphere=sphere)
    await state.set_state(Form.waiting_for_reply)
    # Переводим лид в режим «прямого коридора»
    execute_query("UPDATE leads SET status = 'chatting' WHERE phone = ?", (phone,))
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query("INSERT OR REPLACE INTO chat_sessions (user_id, phone, last_outgoing_at, reminder_sent) VALUES (?, ?, ?, 0)", (c.from_user.id, phone, now_iso))
    # Удаляем старые сообщения предыдущей сессии (если были)
    await chat_history_delete_messages(c.from_user.id, phone=phone)
    row = execute_query("SELECT name FROM leads WHERE phone = ?", (phone,), fetchone=True)
    name = row[0] if row else phone
    # Одна инфо-панель + кнопка «Завершить диалог»
    msg = await c.message.answer(
        f"💬 <b>Диалог с {name}</b> ({phone})\n\nОтправляйте текст, голос, фото — всё уйдёт в WA. Внизу кнопка «Завершить диалог».",
        reply_markup=get_med_finish_dialog_kb(),
        parse_mode="HTML",
    )
    chat_history_add(c.from_user.id, msg.message_id, phone=phone, context="session")
    await c.answer()

@dp.message(Form.waiting_for_reply)
async def reply_done(m: types.Message, state: FSMContext):
    d = await state.get_data()
    # Всегда берём целевой номер из активной сессии — чтобы ответ не ушёл другому клиенту
    session = execute_query("SELECT phone FROM chat_sessions WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    target = session[0] if session else d.get('target')
    if not target:
        return
    sphere = d.get('new_chat_sphere') or get_lead_direction(target)
    if m.text and m.text.strip() == "✅ Завершить диалог":
        # Удаляем все сообщения текущей сессии; остаётся только итог и финальная карточка
        await chat_history_delete_messages(m.from_user.id, phone=target)
        execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, target))
        u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
        if not u:
            return
        if sphere == 'med' and u[1] == 'med':
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ Отказ", callback_data=f"med_r_{target}")
            kb.button(text="⏳ Подумает", callback_data=f"med_t_{target}")
            kb.button(text="💰 Оплатил", callback_data=f"med_p_{target}")
            kb.button(text="🚫 НЕ ОТВЕЧАЮТ", callback_data=f"med_n_{target}")
            kb.adjust(1)
            outcome_msg = await m.answer("Итог диалога:", reply_markup=kb.as_markup())
        else:
            kb = InlineKeyboardBuilder()
            kb.button(text="💰 ОПЛАТИЛ", callback_data=f"f_s_{target}")
            kb.button(text="⏳ ДУМАЕТ", callback_data=f"f_t_{target}")
            kb.button(text="❌ ОТКАЗ", callback_data=f"f_r_{target}")
            kb.button(text="🚫 НЕ ОТВЕТИЛ", callback_data=f"f_n_{target}")
            kb.adjust(1)
            outcome_msg = await m.answer("Итог диалога:", reply_markup=kb.as_markup())
        await state.update_data(outcome_message_id=outcome_msg.message_id, c_phone=target)
        return
    await send_to_wa(target, m, sphere=sphere)
    log_lead_event(target, "outgoing", (m.text or "[медиа]")[:200], m.from_user.id)
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query("INSERT OR REPLACE INTO chat_sessions (user_id, phone, last_outgoing_at, reminder_sent) VALUES (?, ?, ?, 0)", (m.from_user.id, target, now_iso))
    sent = await m.answer("✅ Отправлено!", reply_markup=get_med_finish_dialog_kb())
    chat_history_add(m.from_user.id, sent.message_id, phone=target, context="session")


@dp.message(F.text.contains("Завершить диалог"))
async def finish_dialog_fallback(m: types.Message, state: FSMContext):
    """Резервный обработчик кнопки «Завершить диалог», если FSM-состояние потеряно (перезапуск бота и т.п.)."""
    if await state.get_state() == Form.waiting_for_reply.state:
        return  # уже обработает reply_done
    session = execute_query("SELECT phone FROM chat_sessions WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not session:
        await m.answer("Сейчас нет активного диалога. Начните диалог по лиду через «НАПИСАТЬ».", reply_markup=get_main_menu(m.from_user.id))
        return
    target = session[0]
    sphere = get_lead_direction(target)
    await chat_history_delete_messages(m.from_user.id, phone=target)
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, target))
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    if sphere == 'med' and u[1] == 'med':
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отказ", callback_data=f"med_r_{target}")
        kb.button(text="⏳ Подумает", callback_data=f"med_t_{target}")
        kb.button(text="💰 Оплатил", callback_data=f"med_p_{target}")
        kb.button(text="🚫 НЕ ОТВЕЧАЮТ", callback_data=f"med_n_{target}")
        kb.adjust(1)
        await m.answer("Итог диалога:", reply_markup=kb.as_markup())
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="💰 ОПЛАТИЛ", callback_data=f"f_s_{target}")
        kb.button(text="⏳ ДУМАЕТ", callback_data=f"f_t_{target}")
        kb.button(text="❌ ОТКАЗ", callback_data=f"f_r_{target}")
        kb.button(text="🚫 НЕ ОТВЕТИЛ", callback_data=f"f_n_{target}")
        kb.adjust(1)
        await m.answer("Итог диалога:", reply_markup=kb.as_markup())
    await state.clear()
MED_PACKAGES = ["Пакет 1", "Пакет 2", "Пакет 3", "Первичка", "Вторичка"]

@dp.callback_query(F.data.startswith("med_r_"))
async def med_end_refuse(c: types.CallbackQuery, state: FSMContext):
    phone = c.data[6:]
    if len(phone) > 30:
        phone = phone[:30]
    execute_query("UPDATE leads SET status='closed', is_answered=0 WHERE phone=? AND direction='med'", (phone,))
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (phone,))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (c.from_user.id, phone))
    log_lead_event(phone, "status_change", "closed: Отказ", c.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
    try:
        await c.message.delete()
    except Exception:
        pass
    text, kbd = build_lead_card(phone)
    await c.message.answer(text or "✅ Отказ зафиксирован.", reply_markup=kbd, parse_mode="HTML")
    await c.message.answer("Меню", reply_markup=get_main_menu(c.from_user.id))
    await state.clear()
    await try_assign_queued_lead_to_manager(c.from_user.id, 'med')
    await c.answer()

@dp.callback_query(F.data.startswith("med_t_"))
async def med_end_think(c: types.CallbackQuery, state: FSMContext):
    phone = c.data[6:]
    if len(phone) > 30:
        phone = phone[:30]
    execute_query("UPDATE leads SET status='thinking', is_answered=1, last_touch=? WHERE phone=? AND (direction='med' OR direction IS NULL)", (datetime.now().strftime("%Y-%m-%d %H:%M"), phone))
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (phone,))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (c.from_user.id, phone))
    log_lead_event(phone, "status_change", "thinking: Подумает", c.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
    try:
        await c.message.delete()
    except Exception:
        pass
    text, kbd = build_lead_card(phone)
    await c.message.answer(text or "⏳ Подумает.", reply_markup=kbd, parse_mode="HTML")
    await c.message.answer("Меню", reply_markup=get_main_menu(c.from_user.id))
    await state.clear()
    await try_assign_queued_lead_to_manager(c.from_user.id, 'med')
    await c.answer()

@dp.callback_query(F.data.startswith("med_n_"))
async def med_end_no_answer(c: types.CallbackQuery):
    """Медицина: не ответил — +1 касание, после 7 — автозакрытие."""
    phone = c.data[6:]
    if len(phone) > 20:
        await c.answer()
        return
    row = execute_query("SELECT touches, name FROM leads WHERE phone = ? AND direction = 'med'", (phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    cur_t, name = row[0] or 0, row[1] or phone
    new_t = cur_t + 1
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (c.from_user.id, phone))
    if new_t >= 7:
        execute_query("UPDATE leads SET status='closed', touches=7, comment='Автозакрытие: 7 касаний' WHERE phone=? AND direction='med'", (phone,))
        execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
        try:
            await c.message.delete()
        except Exception:
            pass
        text, kbd = build_lead_card(phone)
        await c.message.answer(text or f"🛑 Лид {name} закрыт (7 касаний).", reply_markup=kbd, parse_mode="HTML")
        await c.message.answer("Меню", reply_markup=get_main_menu(c.from_user.id))
        await try_assign_queued_lead_to_manager(c.from_user.id, 'med')
    else:
        execute_query("UPDATE leads SET touches=?, last_touch=? WHERE phone=? AND direction='med'", (new_t, datetime.now(), phone))
        await c.message.edit_text(f"🔄 Касание №{new_t} зафиксировано. Не ответил.")
    await c.answer()

@dp.callback_query(F.data.startswith("med_p_"))
async def med_end_paid(c: types.CallbackQuery, state: FSMContext):
    phone = c.data[6:]
    if len(phone) > 20:
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    for i, pkg in enumerate(MED_PACKAGES):
        kb.button(text=pkg, callback_data=f"medpkg_{phone}_{i}")
    kb.adjust(2)
    await c.message.edit_text("Выберите услугу/пакет:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("medpkg_"))
async def med_paid_package(c: types.CallbackQuery, state: FSMContext):
    parts = c.data.split("_", 2)
    if len(parts) < 3:
        await c.answer()
        return
    phone = parts[1]
    idx = int(parts[2]) if parts[2].isdigit() else 0
    pkg = MED_PACKAGES[idx] if 0 <= idx < len(MED_PACKAGES) else MED_PACKAGES[0]
    await state.update_data(med_paid_phone=phone, med_paid_service=pkg)
    await state.set_state(Form.med_paid_sum)
    await c.message.edit_text(f"Услуга: {pkg}. Введите сумму оплаты:")
    await c.answer()

@dp.message(Form.med_paid_sum)
async def med_paid_sum_done(m: types.Message, state: FSMContext):
    try:
        s = float(m.text.replace(",", ".").strip())
    except ValueError:
        await m.answer("Введите число (сумма).")
        return
    d = await state.get_data()
    phone = d.get("med_paid_phone")
    service = d.get("med_paid_service", "")
    if not phone:
        await state.clear()
        return
    row = execute_query("SELECT COALESCE(massage_sessions, 0) FROM leads WHERE phone = ? AND direction = 'med'", (phone,), fetchone=True)
    prev_sessions = row[0] if row else 0
    new_sessions = prev_sessions + 1
    execute_query(
        "UPDATE leads SET status='closed', is_answered=1, service=?, payment=COALESCE(payment,0)+?, payment_date=date('now'), massage_sessions=? WHERE phone=? AND direction='med'",
        (service, s, new_sessions, phone),
    )
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (phone,))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, phone))
    log_lead_event(phone, "status_change", f"closed: Оплатил {service} {s}", m.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (m.from_user.id,))
    outcome_mid = d.get("outcome_message_id")
    if outcome_mid:
        try:
            await bot.delete_message(chat_id=m.from_user.id, message_id=outcome_mid)
        except Exception:
            pass
    await m.answer(f"✅ Оплата {s} ({service}) зафиксирована.", reply_markup=get_main_menu(m.from_user.id))
    await state.clear()
    await try_assign_queued_lead_to_manager(m.from_user.id, 'med')
    if new_sessions >= 10:
        kb = InlineKeyboardBuilder().button(text="📞 Предложить Логопеда/Повтор", callback_data=f"med_logoped_{phone}").adjust(1).as_markup()
        await m.answer("Курс завершён (10-й визит). Предложите логопеда или повтор:", reply_markup=kb)

@dp.callback_query(F.data.startswith("med_logoped_"))
async def med_logoped_cb(c: types.CallbackQuery):
    phone = c.data.replace("med_logoped_", "", 1)
    if len(phone) > 30:
        await c.answer()
        return
    log_lead_event(phone, "status_change", "Кнопка: Предложить Логопеда/Повтор", c.from_user.id)
    await c.message.edit_text("✅ Отмечено: предложение логопеда/повтора.")
    await c.answer()

@dp.message(Form.closing_sphere)
async def cl_sph(m: types.Message, state: FSMContext):
    await state.update_data(c_sphere=m.text); await state.set_state(Form.closing_comment); await m.answer("Комментарий:")

@dp.message(Form.closing_comment)
async def cl_fin(m: types.Message, state: FSMContext):
    d = await state.get_data()
    st = "thinking" if d['c_status'] == 't' else "closed"
    ans = 1 if d['c_status'] in ['s', 't', 'r'] else 0
    execute_query("UPDATE leads SET status=?, sphere=?, comment=?, is_answered=? WHERE phone=?", (st, d['c_sphere'], m.text, ans, d['c_phone']))
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (d['c_phone'],))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, d['c_phone']))
    log_lead_event(d['c_phone'], "status_change", f"{st}: {m.text[:100]}", m.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (m.from_user.id,))
    outcome_mid = d.get("outcome_message_id")
    if outcome_mid:
        try:
            await bot.delete_message(chat_id=m.from_user.id, message_id=outcome_mid)
        except Exception:
            pass
    text, kbd = build_lead_card(d['c_phone'])
    await m.answer(text or "✅ Отчет принят!", reply_markup=kbd, parse_mode="HTML")
    await m.answer("Меню", reply_markup=get_main_menu(m.from_user.id))
    await state.clear()
    u = execute_query("SELECT sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    mgr_sphere = (u[0] or 'biz') if u else 'biz'
    await try_assign_queued_lead_to_manager(m.from_user.id, mgr_sphere)

@dp.message(F.text == "🎯 Поставить План")
async def p_list(m: types.Message, state: FSMContext):
    data = await state.get_data()
    sphere = data.get("owner_current_sphere")  # med | biz — из меню владельца
    if sphere == "med":
        st = execute_query("SELECT user_id, fio, sphere FROM users WHERE role='manager' AND sphere='med'", fetchall=True)
    else:
        st = execute_query("SELECT user_id, fio, sphere FROM users WHERE role='manager' AND (sphere='biz' OR sphere IS NULL)", fetchall=True)
    if not st:
        await m.answer("Нет менеджеров в выбранном направлении.")
        return
    kb = InlineKeyboardBuilder()
    for row in st:
        sid, fio = row[0], row[1]
        sph = (row[2] or 'биз') if len(row) > 2 else 'биз'
        kb.button(text=f"🎯 {fio} ({sph})", callback_data=f"sp_{sid}")
    await m.answer("Кому план?", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("sp_"))
async def p_val(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(tm=c.data.split("_")[1]); await state.set_state(Form.setting_plan_value); await c.message.answer("Введите план:"); await c.answer()

@dp.message(Form.setting_plan_value)
async def p_save(m: types.Message, state: FSMContext):
    d = await state.get_data(); execute_query("UPDATE users SET plan = ? WHERE user_id = ?", (m.text, d['tm']))
    await m.answer(f"✅ План {m.text} сохранен!"); await state.clear()

@dp.message(F.text.in_(["⏳ Дожим", "✅ Оплачено", "❌ Отказ"]))
async def mgr_lists(m: types.Message):
    st_map = {"⏳ Дожим": "thinking", "✅ Оплачено": "closed", "❌ Отказ": "closed"}
    leads = execute_query("SELECT phone, name FROM leads WHERE manager_id = ? AND status = ? AND (direction = 'biz' OR direction IS NULL)", (m.from_user.id, st_map[m.text]), fetchall=True)
    if not leads: return await m.answer("Список пуст.")
    kb = InlineKeyboardBuilder()
    for p, n in leads: kb.button(text=f"👤 {n}", callback_data=f"rp_{p}")
    await m.answer(f"Ваши клиенты ({m.text}):", reply_markup=kb.adjust(1).as_markup())

@dp.message(F.text.contains("ЛИДЫ"))
async def tgl(m: types.Message):
    c = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
    v = '0' if c and c[0] == '1' else '1'; execute_query("INSERT OR REPLACE INTO settings (key, value) VALUES ('leads_enabled', ?)", (v,))
    await m.answer(f"Статус: {'ВКЛ' if v=='1' else 'ВЫКЛ'}", reply_markup=get_main_menu(m.from_user.id))

@dp.message(F.text == "📂 Загрузка данных")
async def dl(m: types.Message):
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "crm.xlsx")
    with sqlite3.connect(DB_PATH) as conn:
        pd.read_sql_query("SELECT * FROM leads WHERE (direction = 'biz' OR direction IS NULL)", conn).to_excel(path, index=False)
    await m.answer_document(types.FSInputFile(path))

@dp.message(F.text == "📋 Лиды в работе")
async def wl(m: types.Message, state: FSMContext):
    data = await state.get_data()
    sphere = data.get("owner_current_sphere")
    if is_owner(m.from_user.id) and sphere == "med":
        cond = "status IN ('active', 'chatting') AND direction = 'med'"
        title = "📋 <b>В РАБОТЕ (Медицина):</b>"
    else:
        cond = "status IN ('active', 'chatting') AND (direction = 'biz' OR direction IS NULL)"
        title = "📋 <b>В РАБОТЕ (Бизнес):</b>"
    rows = execute_query(
        f"SELECT l.phone, l.name, l.manager_id, l.status FROM leads l WHERE {cond} ORDER BY l.last_touch DESC",
        fetchall=True,
    )
    if not rows:
        await m.answer(title + "\n\nПусто", parse_mode="HTML")
        return
    lines = [title + "\n"]
    kb = InlineKeyboardBuilder()
    for phone, name, mgr_id, st in rows:
        fio = "—"
        if mgr_id:
            u = execute_query("SELECT fio FROM users WHERE user_id = ?", (mgr_id,), fetchone=True)
            fio = (u[0] or str(mgr_id)) if u else str(mgr_id)
        status_label = "в диалоге" if st == "chatting" else "активен"
        lines.append(f"👤 <b>{name}</b> ({phone})\n   Ответственный: {fio} [{status_label}]")
        kb.button(text=f"↩ Перенаправить: {name}", callback_data=f"reas_{phone}")
    await m.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.adjust(1).as_markup())


@dp.callback_query(F.data.startswith("reas_"))
async def reassign_lead_choose(c: types.CallbackQuery, state: FSMContext):
    """Выбор менеджера для перенаправления лида."""
    if not is_owner(c.from_user.id):
        await c.answer()
        return
    phone = c.data.replace("reas_", "")[:30]
    row = execute_query("SELECT name, manager_id, direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    name, old_mgr_id, direction = row[0], row[1], (row[2] or "biz")
    await state.update_data(reassign_phone=phone, reassign_old_mgr=old_mgr_id, reassign_direction=direction)
    managers = get_managers_by_direction(direction)
    kb = InlineKeyboardBuilder()
    for uid, fio in managers:
        if uid != old_mgr_id:
            kb.button(text=f"→ {fio}", callback_data=f"reto_{phone}_{uid}")
    if not kb.buttons:
        await c.answer("Нет других менеджеров в этом направлении.")
        return
    await c.message.answer(f"Кому перенаправить лида <b>{name}</b> ({phone})?", reply_markup=kb.adjust(1).as_markup(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("reto_"))
async def reassign_lead_do(c: types.CallbackQuery, state: FSMContext):
    """Перенаправить лид другому менеджеру и удалить из чата старого."""
    if not is_owner(c.from_user.id):
        await c.answer()
        return
    parts = c.data.split("_")
    if len(parts) < 3:
        await c.answer()
        return
    phone = parts[1][:30]
    new_mgr_id = int(parts[2])
    row = execute_query("SELECT name, manager_id FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    name, old_mgr_id = row[0], row[1]
    execute_query("UPDATE leads SET manager_id = ? WHERE phone = ?", (new_mgr_id, phone))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (old_mgr_id, phone))
    if old_mgr_id:
        await chat_history_delete_messages(old_mgr_id, phone=phone)
        try:
            await bot.send_message(old_mgr_id, f"📤 Лид <b>{name}</b> ({phone}) перенаправлен другому менеджеру.", parse_mode="HTML")
        except Exception:
            pass
    kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone}").adjust(2).as_markup()
    try:
        await bot.send_message(new_mgr_id, f"📥 <b>Лида перенаправили вам</b>\n👤 {name}\n📞 {phone}", reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await c.message.edit_text(f"✅ Лид {name} ({phone}) перенаправлен.")
    await state.clear()
    await c.answer()

# ========== МЕДИЦИНА: меню владельца и админа ==========
@dp.message(F.text == "📊 Нагруженность")
async def med_load(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] != 'owner' and not (u[0] == 'admin' and u[1] == 'med')):
        return
    today = date.today().strftime("%Y-%m-%d")
    a_cnt = appointment_count(today, 'assistant')
    g_cnt = appointment_count(today, 'ganchina')
    await m.answer(f"📊 <b>Нагруженность на сегодня</b> ({today})\n\nАссистент (первичка): {a_cnt}/10\nГанчина (вторичка): {g_cnt}/5", parse_mode="HTML")

@dp.message(F.text == "📈 Статистика лидов")
async def med_stats_menu(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'owner':
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="День", callback_data="medstat_day")
    kb.button(text="Неделя", callback_data="medstat_week")
    kb.button(text="Месяц", callback_data="medstat_month")
    await m.answer("Статистика лидов (Медицина). Период:", reply_markup=kb.adjust(3).as_markup())

@dp.callback_query(F.data.startswith("medstat_"))
async def med_stats_cb(c: types.CallbackQuery):
    period = c.data.replace("medstat_", "")
    now = datetime.now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    cond = " direction = 'med' "
    all_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE {cond} AND (last_touch >= ? OR payment_date >= ?)", (start_str, start_str[:10]), fetchone=True)[0] or 0
    ans_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE {cond} AND is_answered=1", fetchone=True)[0]
    sold_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE {cond} AND status='closed' AND is_answered=1 AND comment NOT LIKE '%Автозакрытие%'", fetchone=True)[0]
    c_ans = round((ans_l / (all_l or 1)) * 100, 1)
    c_sale = round((sold_l / (ans_l or 1)) * 100, 1)
    await c.message.edit_text(f"📈 <b>Медицина</b> ({period})\n\nПришло: {all_l}\nОбработано (дозвон): {ans_l} ({c_ans}%)\nПродано: {sold_l} ({c_sale}%)", parse_mode="HTML")
    await c.answer()

@dp.message(F.text == "👑 Дать права владельца")
async def give_owner_btn(m: types.Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return
    await state.clear()
    admins = execute_query(
        "SELECT user_id, fio, sphere FROM users WHERE role = 'admin' ORDER BY sphere, fio",
        fetchall=True,
    )
    if not admins:
        await m.answer("Нет администраторов. Сначала назначьте админов (Бизнес или Медицина) — владельца можно выбрать только из них.")
        return
    kb = InlineKeyboardBuilder()
    for uid, fio, sphere in admins:
        label = fio or str(uid)
        sphere_label = "бизнес" if sphere == "biz" else "медицина" if sphere == "med" else ""
        if sphere_label:
            label += f" ({sphere_label})"
        kb.button(text=f"👤 {label}", callback_data=f"giveowner_{uid}")
    await m.answer("Кого сделать владельцем? (выбор из администраторов)", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("giveowner_"))
async def give_owner_cb(c: types.CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer()
        return
    uid = int(c.data.split("_")[1])
    row = execute_query("SELECT fio, role FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not row or row[1] != "admin":
        await c.answer("Пользователь не найден или не админ.")
        return
    fio = row[0] or str(uid)
    execute_query("UPDATE users SET role = 'owner', sphere = NULL WHERE user_id = ?", (uid,))
    try:
        await bot.send_message(uid, "✅ Вам выданы права владельца CRM. Нажмите /start для обновления меню.")
    except Exception:
        pass
    await c.message.edit_text(f"✅ {fio} назначен владельцем.")
    await c.answer()

@dp.message(F.text == "👤 Назначить Админа")
async def med_assign_admin(m: types.Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return
    await state.clear()
    st = execute_query(
        "SELECT user_id, fio FROM users WHERE role = 'manager' AND sphere = 'med'",
        fetchall=True,
    )
    if not st:
        await m.answer("Нет менеджеров медицины для назначения админа.")
        return
    kb = InlineKeyboardBuilder()
    for uid, fio in st:
        kb.button(text=f"👤 {fio or str(uid)}", callback_data=f"medadm_{uid}")
    await m.answer("Кого сделать админом медицины?", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("medadm_"))
async def med_assign_admin_cb(c: types.CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer()
        return
    uid = int(c.data.split("_")[1])
    row = execute_query("SELECT fio FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not row:
        await c.answer("Пользователь не найден.")
        return
    fio = row[0] or str(uid)
    execute_query("UPDATE users SET role = 'admin', sphere = 'med' WHERE user_id = ?", (uid,))
    try:
        await bot.send_message(uid, "✅ Вам выданы права Админа Медицины. Нажмите /start.")
    except Exception:
        pass
    await c.message.edit_text(f"✅ {fio} назначен админом медицины.")
    await c.answer()

@dp.message(F.text == "👤 Назначить админа бизнеса")
async def biz_assign_admin(m: types.Message):
    if not is_owner(m.from_user.id):
        return
    st = execute_query(
        "SELECT user_id, fio FROM users WHERE role='manager' AND (sphere = 'biz' OR sphere IS NULL)",
        fetchall=True,
    )
    if not st:
        await m.answer("Нет менеджеров бизнеса для назначения админа.")
        return
    kb = InlineKeyboardBuilder()
    for uid, fio in st:
        kb.button(text=f"👤 {fio}", callback_data=f"bizadm_{uid}")
    await m.answer("Кого сделать админом бизнеса?", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("bizadm_"))
async def biz_assign_admin_cb(c: types.CallbackQuery):
    if not is_owner(c.from_user.id):
        await c.answer()
        return
    uid = int(c.data.split("_")[1])
    row = execute_query("SELECT fio FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if not row:
        await c.answer("Пользователь не найден.")
        return
    fio = row[0]
    execute_query("UPDATE users SET role='admin', sphere='biz' WHERE user_id = ?", (uid,))
    try:
        await bot.send_message(uid, "✅ Вам выданы права Админа Бизнеса. Нажмите /start.")
    except Exception:
        pass
    await c.message.edit_text(f"✅ {fio} назначен админом бизнеса.")
    await c.answer()

@dp.message(F.text == "📂 Выгрузка (мед)")
async def dl_med(m: types.Message):
    import tempfile
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] != 'owner' and not (u[0] == 'admin' and u[1] == 'med')):
        return
    path = os.path.join(tempfile.gettempdir(), "crm_med.xlsx")
    with sqlite3.connect(DB_PATH) as conn:
        pd.read_sql_query("SELECT name AS ФИО, phone AS Телефон, service AS Услуга, payment AS Сумма FROM leads WHERE direction = 'med'", conn).to_excel(path, index=False)
    await m.answer_document(types.FSInputFile(path))

@dp.message(F.text == "📂 Загрузка данных")
async def dl(m: types.Message):
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "crm.xlsx")
    with sqlite3.connect(DB_PATH) as conn:
        pd.read_sql_query("SELECT * FROM leads WHERE (direction = 'biz' OR direction IS NULL)", conn).to_excel(path, index=False)
    await m.answer_document(types.FSInputFile(path))

@dp.message(F.text == "💰 Приход")
async def med_income_menu(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] != 'owner' and not (u[0] == 'admin' and u[1] == 'med')):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="День", callback_data="medinc_day")
    kb.button(text="Неделя", callback_data="medinc_week")
    kb.button(text="Месяц", callback_data="medinc_month")
    await m.answer("Приход (Медицина). Период:", reply_markup=kb.adjust(3).as_markup())

@dp.callback_query(F.data.startswith("medinc_"))
async def med_income_cb(c: types.CallbackQuery):
    period = c.data.replace("medinc_", "")
    now = date.today()
    if period == "day":
        start = now
        end = now
    elif period == "week":
        start = now - timedelta(days=7)
        end = now
    else:
        start = now - timedelta(days=30)
        end = now
    cash = execute_query("SELECT COALESCE(SUM(payment), 0) FROM leads WHERE direction = 'med' AND payment_date >= ? AND payment_date <= ?", (start.isoformat(), end.isoformat()), fetchone=True)[0] or 0
    debt = execute_query("SELECT COALESCE(SUM(debt), 0) FROM leads WHERE direction = 'med'", fetchone=True)[0] or 0
    await c.message.edit_text(f"💰 <b>Приход (Медицина)</b> — {period}\n\nКассовый приход: {cash}\nДебиторка (всего): {debt}", parse_mode="HTML")
    await c.answer()

@dp.message(F.text.startswith("📥 Мои лиды"))
async def toggle_admin_self_leads(m: types.Message):
    u = execute_query("SELECT can_receive_leads FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    cur = u[0] or 0
    new = 0 if cur else 1
    execute_query("UPDATE users SET can_receive_leads = ? WHERE user_id = ?", (new, m.from_user.id))
    status = "ВКЛ" if new else "ВЫКЛ"
    await m.answer(f"Лиды для вас: {status}", reply_markup=get_main_menu(m.from_user.id))

@dp.message(F.text == "🎯 План/KPI")
async def med_plan_kpi(m: types.Message):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'owner':
        return
    st = execute_query("SELECT user_id, fio, plan FROM users WHERE role='manager' AND sphere='med'", fetchall=True)
    txt = "🎯 <b>План/KPI (Медицина)</b>\n\nПоказатели: План, Дозвон, Касания.\n\n"
    for mid, fio, plan in st:
        m_all = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND direction='med'", (mid,), fetchone=True)[0] or 1
        m_ans = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND direction='med' AND is_answered=1", (mid,), fetchone=True)[0]
        m_t = execute_query("SELECT AVG(touches) FROM leads WHERE manager_id=? AND direction='med'", (mid,), fetchone=True)[0] or 0
        conv = round((m_ans / m_all) * 100, 1)
        txt += f"▪️ <b>{fio}</b>  План: {plan}  Дозвон: {conv}%  Ср. касаний: {round(m_t, 1)}\n\n"
    await m.answer(txt or "Нет менеджеров медицины.", parse_mode="HTML")

@dp.message(F.text == "📥 Поступления лидов (мед)")
async def med_leads_flow_menu(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    if not (role == 'owner' or (role == 'admin' and sphere == 'med')):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="День", callback_data="medlead_day")
    kb.button(text="Неделя", callback_data="medlead_week")
    kb.button(text="Месяц", callback_data="medlead_month")
    kb.button(text="Другая дата/период", callback_data="medlead_custom")
    await m.answer("Поступления лидов (Медицина). Период:", reply_markup=kb.adjust(2).as_markup())

@dp.callback_query(F.data.startswith("medlead_"))
async def med_leads_flow_cb(c: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(c)  # сразу, чтобы не было "query is too old"
    period = c.data.replace("medlead_", "")
    if period == "custom":
        await state.set_state(Form.med_leads_custom_period)
        await c.message.answer(
            "Введите дату или период:\n"
            "- один день: 2026-02-24\n"
            "- период: 2026-02-01 2026-02-24"
        )
        return
    now = datetime.now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)
    end = now
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")
    cond = " direction = 'med' "
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND is_answered = 1 AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    conv = round((processed / (total or 1)) * 100, 1)
    await c.message.edit_text(
        f"📥 <b>Поступления лидов (Медицина)</b> — {period}\n\n"
        f"Пришло лидов: {total}\n"
        f"Обработано: {processed} ({conv}%)",
        parse_mode="HTML",
    )

@dp.message(Form.med_leads_custom_period)
async def med_leads_flow_custom_period(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    if not (role == 'owner' or (role == 'admin' and sphere == 'med')):
        return
    parts = m.text.strip().split()
    try:
        if len(parts) == 1:
            d1 = datetime.strptime(parts[0], "%Y-%m-%d").date()
            d2 = d1
        elif len(parts) == 2:
            d1 = datetime.strptime(parts[0], "%Y-%m-%d").date()
            d2 = datetime.strptime(parts[1], "%Y-%m-%d").date()
        else:
            raise ValueError()
    except ValueError:
        await m.answer("Неверный формат. Пример:\n2026-02-24\nили\n2026-02-01 2026-02-24")
        return
    if d2 < d1:
        d1, d2 = d2, d1
    start_dt = datetime.combine(d1, datetime.min.time())
    end_dt = datetime.combine(d2, datetime.max.time()).replace(microsecond=0)
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    cond = " direction = 'med' "
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND is_answered = 1 AND last_touch BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    conv = round((processed / (total or 1)) * 100, 1)
    label = d1.isoformat() if d1 == d2 else f"{d1.isoformat()}—{d2.isoformat()}"
    await m.answer(
        f"📥 <b>Поступления лидов (Медицина)</b> — {label}\n\n"
        f"Пришло лидов: {total}\n"
        f"Обработано: {processed} ({conv}%)",
        parse_mode="HTML",
    )
    await state.clear()

@dp.message(F.text == "📅 Записать к Ассистенту")
async def med_appoint_assistant(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] not in ('owner', 'admin') or u[1] != 'med'):
        return
    await state.update_data(med_doctor='assistant')
    await state.set_state(Form.med_appoint_date)
    await m.answer("Введите дату записи (ГГГГ-ММ-ДД):")

@dp.message(F.text == "📅 Записать к Ганчине")
async def med_appoint_ganchina(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] not in ('owner', 'admin') or u[1] != 'med'):
        return
    await state.update_data(med_doctor='ganchina')
    await state.set_state(Form.med_appoint_date)
    await m.answer("Введите дату записи (ГГГГ-ММ-ДД):")

@dp.message(Form.med_appoint_date)
async def med_appoint_date_ok(m: types.Message, state: FSMContext):
    d = await state.get_data()
    doctor = d.get('med_doctor', 'assistant')
    ok, err = can_add_appointment(m.text.strip(), doctor)
    if not ok:
        await m.answer(err or "Формат даты: ГГГГ-ММ-ДД")
        return
    await state.update_data(med_date=m.text.strip())
    await state.set_state(Form.med_appoint_time)
    await m.answer("Введите время (ЧЧ:ММ):")

@dp.message(Form.med_appoint_time)
async def med_appoint_time_ok(m: types.Message, state: FSMContext):
    t = m.text.strip()
    parts = t.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await m.answer("Введите время в формате ЧЧ:ММ")
        return
    await state.update_data(med_time=t)
    await state.set_state(Form.med_appoint_phone)
    await m.answer("Введите номер телефона пациента (без +):")

@dp.message(Form.med_appoint_phone)
async def med_appoint_phone_ok(m: types.Message, state: FSMContext):
    phone = m.text.strip().replace("+", "").replace(" ", "")
    data = await state.get_data()
    date_str = data['med_date']
    doctor = data.get('med_doctor', 'assistant')
    ok, err = can_add_appointment(date_str, doctor)
    if not ok:
        await m.answer(err)
        return
    execute_query("INSERT INTO appointments (date, time, doctor, phone) VALUES (?, ?, ?, ?)", (date_str, data['med_time'], doctor, phone))
    await m.answer(f"✅ Запись: {date_str} {data['med_time']}, {doctor}, {phone}")
    await state.clear()

@dp.message(F.text == "📋 Мои записи")
async def med_my_records(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[1] != 'med':
        return
    if u[0] == 'manager':
        leads = execute_query("SELECT phone, name FROM leads WHERE manager_id = ? AND direction = 'med'", (m.from_user.id,), fetchall=True)
        if not leads:
            await m.answer("Нет ваших пациентов (лидов).")
            return
        kb = InlineKeyboardBuilder()
        for phone, name in leads:
            kb.button(text=f"✍️ {name}", callback_data=f"rp_{phone}")
            kb.button(text=f"📞 {name}", callback_data=f"cl_{phone}")
        kb.adjust(2)
        await m.answer("📋 Ваши пациенты:", reply_markup=kb.as_markup())
    else:
        rows = execute_query("SELECT date, time, doctor, phone FROM appointments WHERE date >= date('now') ORDER BY date, time", fetchall=True)
        if not rows:
            await m.answer("Нет предстоящих записей.")
            return
        txt = "📋 Записи:\n\n" + "\n".join([f"{r[0]} {r[1]} — {r[2]}, {r[3]}" for r in rows])
        await m.answer(txt)

@dp.message(F.text == "💰 Мои оплаты")
async def med_my_payments(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'manager' or u[1] != 'med':
        return
    rows = execute_query(
        "SELECT name, phone, COALESCE(payment, 0) FROM leads WHERE manager_id = ? AND direction = 'med' AND COALESCE(payment, 0) > 0",
        (m.from_user.id,),
        fetchall=True,
    )
    if not rows:
        await m.answer("Нет оплат по вашим пациентам.")
        return
    txt = "💰 <b>Мои оплаты</b> (сумма обновляется при вводе админом):\n\n" + "\n".join([f"👤 {r[0]} ({r[1]}): {r[2]} ₽" for r in rows])
    await m.answer(txt, parse_mode="HTML")

@dp.message(F.text == "⏳ Дожим")
async def med_dozhim(m: types.Message):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'manager' or u[1] != 'med':
        return
    leads = execute_query("SELECT phone, name FROM leads WHERE manager_id = ? AND status = 'thinking' AND (direction = 'med' OR direction IS NULL)", (m.from_user.id,), fetchall=True)
    if not leads:
        await m.answer("В дожиме никого нет.")
        return
    kb = InlineKeyboardBuilder()
    for phone, name in leads:
        kb.button(text=f"👤 {name}", callback_data=f"rp_{phone}")
        kb.button(text=f"📞 {name}", callback_data=f"cl_{phone}")
    kb.adjust(1)
    await m.answer("⏳ Дожим — ваши пациенты:", reply_markup=kb.as_markup())

@dp.message(F.text == "💰 Оплаты")
async def med_payment_start(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] not in ('owner', 'admin') or u[1] != 'med'):
        return
    await state.set_state(Form.med_payment_phone)
    await m.answer("Введите номер телефона пациента:")

@dp.message(Form.med_payment_phone)
async def med_payment_phone_ok(m: types.Message, state: FSMContext):
    await state.update_data(med_p_phone=m.text.strip().replace("+", ""))
    await state.set_state(Form.med_payment_sum)
    await m.answer("Введите сумму оплаты:")

@dp.message(Form.med_payment_sum)
async def med_payment_sum_ok(m: types.Message, state: FSMContext):
    try:
        s = float(m.text.replace(",", ".").strip())
    except ValueError:
        await m.answer("Введите число.")
        return
    d = await state.get_data()
    phone = d['med_p_phone']
    execute_query("UPDATE leads SET payment = COALESCE(payment, 0) + ?, payment_date = date('now') WHERE phone = ? AND direction = 'med'", (s, phone))
    await m.answer(f"✅ Оплата {s} зафиксирована для {phone}.")
    await state.clear()

@dp.message(F.text == "🔄 Продлить курс")
async def med_extend_start(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] not in ('owner', 'admin') or u[1] != 'med'):
        return
    await state.set_state(Form.med_extend_phone)
    await m.answer("Введите номер телефона пациента для продления курса массажа:")

@dp.message(Form.med_extend_phone)
async def med_extend_done(m: types.Message, state: FSMContext):
    phone = m.text.strip().replace("+", "")
    row = execute_query("SELECT massage_sessions FROM leads WHERE phone = ? AND direction = 'med'", (phone,), fetchone=True)
    if not row:
        execute_query("INSERT INTO leads (phone, name, status, manager_id, direction, massage_sessions) VALUES (?, 'Пациент', 'active', ?, 'med', 1)", (phone, get_first_owner_id()))
        new_val = 1
    else:
        execute_query("UPDATE leads SET massage_sessions = COALESCE(massage_sessions, 0) + 1 WHERE phone = ? AND direction = 'med'", (phone,))
        new_val = (execute_query("SELECT massage_sessions FROM leads WHERE phone = ? AND direction = 'med'", (phone,), fetchone=True) or [0])[0]
    await m.answer(f"✅ Курс продлен. Сеансов массажа: {new_val}")
    if new_val >= 10:
        admins = execute_query("SELECT user_id FROM users WHERE role = 'admin' AND sphere = 'med'", fetchall=True)
        for (aid,) in admins:
            try:
                await bot.send_message(aid, f"🔔 Кросс-продажа: пациент {phone} завершил 10-й сеанс массажа. Предложите Логопеда.")
            except Exception:
                pass
    await state.clear()

@dp.message(Command("debug_med"))
async def debug_med(m: types.Message):
    """Проверка: почему лиды с медицины не приходят. Только владелец."""
    if not is_owner(m.from_user.id):
        return
    med_mgrs = execute_query("SELECT user_id, fio, sphere FROM users WHERE LOWER(role)='manager' AND sphere = 'med'", fetchall=True)
    in_queue = execute_query("SELECT phone, step, instance_sphere FROM auth_queue WHERE instance_sphere = 'med'", fetchall=True)
    med_leads = execute_query("SELECT phone, name, manager_id FROM leads WHERE direction = 'med' ORDER BY last_touch DESC LIMIT 5", fetchall=True)
    lines = ["🔍 <b>DEBUG: Медицина</b>\n"]
    if not med_mgrs:
        lines.append("❌ <b>Мед. менеджеров нет.</b>\nЛиды с мед. WA будут приходить только тебе.\nДобавь сотрудника: заявка в боте → кнопка 🏥 МЕДИЦИНА.")
    else:
        lines.append("✅ Мед. менеджеры (им должны приходить лиды):")
        for uid, fio, sph in med_mgrs:
            lines.append(f"  • {fio} (ID: {uid})")
    lines.append(f"\n📥 В очереди (ждут имя): {len(in_queue)}")
    if in_queue:
        for ph, step, inst in in_queue[:5]:
            lines.append(f"  {ph} step={step}")
    lines.append(f"\n📋 Последние мед. лиды: {len(med_leads)}")
    for ph, name, mid in med_leads:
        lines.append(f"  {name} {ph} → manager_id={mid}")
    await m.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("clear_db"))
async def clear_db(m: types.Message):
    if is_owner(m.from_user.id):
        execute_query("DELETE FROM leads"); execute_query("DELETE FROM auth_queue"); execute_query("UPDATE users SET is_busy = 0")
        await m.answer("🧹 База очищена!")

async def main():
    global g_http_session
    g_http_session = aiohttp.ClientSession()
    init_db()
    med_mgrs = execute_query("SELECT user_id, fio, sphere FROM users WHERE LOWER(role)='manager' AND sphere = 'med'", fetchall=True)
    msg = f"[CRM] Med managers (получают лиды с мед. WA): {med_mgrs if med_mgrs else 'НЕТ — добавьте менеджера через заявку и кнопку МЕДИЦИНА'}"
    logging.info("%s", msg)
    print(msg)
    print(f"[CRM] MED Green API: idInstance={MED_ID_INSTANCE}, url={MED_API_URL} (опрос каждую сек)")
    logging.info("MED Green API: idInstance=%s", MED_ID_INSTANCE)
    asyncio.create_task(check_wa_biz())
    asyncio.create_task(check_wa_med())
    asyncio.create_task(job_remind_24h())
    asyncio.create_task(job_chatting_idle())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())