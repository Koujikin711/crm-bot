import logging
import asyncio
import json
import aiohttp
import sqlite3
import os
import tempfile
import pandas as pd
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    API_TOKEN, ID_INSTANCE, API_TOKEN_INSTANCE, API_URL,
    MED_ID_INSTANCE, MED_API_TOKEN, MED_API_URL,
    DB_PATH, OWNER_ID, DOCTOR_LIMITS, CHATTING_IDLE_MINUTES,
    REMINDER_24H_HOURS, REMINDER_24H_WINDOW_HOURS,
    TELEGRAM_LEADS_PHONE, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH,
    GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB_NAME, GOOGLE_CREDENTIALS_JSON,
)
from db import execute_query, init_db

# Условие «лид бизнеса» в SQL (включая пустой direction)
BIZ_LEAD_COND = "(direction = 'biz' OR direction IS NULL OR TRIM(COALESCE(direction,''))='')"
# Тестовые лиды (phone LIKE 'TEST_%') не учитываются в статистике
NOT_TEST_LEAD_COND = " AND (phone NOT LIKE 'TEST_%')"


def _append_biz_lead_to_sheet(date_str, fio, phone, vid_biznesa, bol_klienta, kommentariy, perezvon):
    """Добавить строку в Google Sheet «База данных лидов». Вызывать из потока (sync)."""
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        return
    try:
        import base64
        import gspread
        from google.oauth2.service_account import Credentials
        creds_str = GOOGLE_CREDENTIALS_JSON.strip().strip('"').strip("'")
        if not creds_str:
            logging.warning("CRM: GOOGLE_CREDENTIALS_JSON пустой — запись в таблицу отключена.")
            return
        info = None
        # Сначала Base64 (так обычно задают в переменных окружения)
        if not creds_str.startswith("{"):
            try:
                b64 = creds_str.replace("\n", "").replace(" ", "").replace("\r", "")
                decoded = base64.b64decode(b64).decode("utf-8")
                info = json.loads(decoded)
            except Exception:
                pass
        if info is None:
            try:
                info = json.loads(creds_str)
            except json.JSONDecodeError:
                pass
        if info is None:
            logging.warning(
                "CRM: GOOGLE_CREDENTIALS_JSON не распознан. Задайте ключ в Base64: "
                "python to_base64_creds.py ключ.json → скопировать вывод в переменную (без кавычек)."
            )
            return
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        wks = sh.worksheet(GOOGLE_SHEET_TAB_NAME)
        row = [date_str, fio or "", phone or "", vid_biznesa or "", bol_klienta or "", kommentariy or "", perezvon or ""]
        wks.append_row(row, value_input_option="USER_ENTERED")
        logging.info("CRM: appended biz lead row to Google Sheet: %s", phone)
    except Exception as e:
        logging.warning("CRM: Google Sheet append failed (check GOOGLE_SHEET_ID and service account access): %s", e)


try:
    import tg_leads_client as _tg_leads
except ImportError:
    _tg_leads = None

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
    """После закрытия лида: если есть лид в очереди (status=pending), назначить его этому менеджеру. Для бизнеса — только если глобально лиды включены."""
    if is_owner(manager_id):
        return
    if direction != 'med':
        l_on = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
        if l_on is not None and str(l_on[0]).strip() == '0':
            return
    if direction == 'med':
        cond = "direction = 'med'"
    else:
        cond = BIZ_LEAD_COND
    row = execute_query(
        f"SELECT phone, name FROM leads WHERE status = 'pending' AND {cond} ORDER BY last_touch ASC LIMIT 1",
        (),
        fetchone=True,
    )
    if not row:
        logging.info("[CRM] try_assign: нет лидов в очереди (direction=%s), менеджер %s", direction, manager_id)
        return
    phone, name = row[0], row[1] or row[0]
    # Атомарно занять лид: только если он всё ещё pending (иначе другой менеджер уже взял)
    execute_query("UPDATE leads SET manager_id = ?, status = 'active' WHERE phone = ? AND status = 'pending'", (manager_id, phone))
    check = execute_query("SELECT manager_id FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not check or check[0] != manager_id:
        logging.info("[CRM] try_assign: лид %s уже взят другим менеджером, пропуск для %s", phone, manager_id)
        return
    execute_query("UPDATE users SET is_busy = 1 WHERE user_id = ?", (manager_id,))
    kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
    try:
        await bot.send_message(manager_id, f"📥 <b>Следующий лид из очереди</b>\n👤 {name}\n📞 {phone}", reply_markup=kb, parse_mode="HTML")
        logging.info("[CRM] try_assign: лид %s назначен менеджеру %s (direction=%s)", phone, manager_id, direction)
    except Exception as e:
        logging.exception("[CRM] try_assign: не удалось отправить лид менеджеру %s: %s", manager_id, e)
        execute_query("UPDATE leads SET manager_id = NULL, status = 'pending' WHERE phone = ?", (phone,))
        execute_query("UPDATE users SET is_busy = 0 WHERE user_id = ?", (manager_id,))

async def distribute_pending_biz_leads() -> int:
    """Распределить все pending-лиды бизнеса по свободным менеджерам. Возвращает число назначенных."""
    cond = BIZ_LEAD_COND
    assigned = 0
    while True:
        row = execute_query(
            f"SELECT phone, name FROM leads WHERE status = 'pending' AND {cond} ORDER BY last_touch ASC LIMIT 1",
            (),
            fetchone=True,
        )
        if not row:
            break
        phone, name = row[0], row[1] or row[0]
        target = get_free_manager_for_direction('biz')
        if not target:
            logging.info("[CRM] distribute_pending_biz: свободных менеджеров нет, осталось в очереди")
            break
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("UPDATE leads SET manager_id = ?, status = 'active', last_touch = ? WHERE phone = ?", (target, now_iso, phone))
        execute_query("UPDATE users SET is_busy = 1 WHERE user_id = ?", (target,))
        kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
        try:
            await bot.send_message(target, f"📥 <b>Лид из очереди</b>\n👤 {name}\n📞 {phone}", reply_markup=kb, parse_mode="HTML")
            assigned += 1
            logging.info("[CRM] distribute_pending_biz: лид %s -> менеджер %s", phone, target)
        except Exception as e:
            logging.exception("[CRM] distribute_pending_biz: отправка менеджеру %s: %s", target, e)
            execute_query("UPDATE leads SET manager_id = NULL, status = 'pending' WHERE phone = ?", (phone,))
            execute_query("UPDATE users SET is_busy = 0 WHERE user_id = ?", (target,))
            break
    return assigned

def get_free_manager_for_direction(direction: str):
    """Менеджер свободен: нет лидов active/chatting за ним (is_busy не учитываем — может застрять). Для бизнеса учитывается глобальный leads_enabled (по умолчанию вкл)."""
    if direction == 'med':
        role_cond = "LOWER(role)='manager' AND sphere='med'"
        lead_cond = "direction = 'med'"
    else:
        l_on = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
        if l_on is not None and str(l_on[0]).strip() == '0':
            return None
        role_cond = "(LOWER(u.role)='manager' AND (u.sphere='biz' OR u.sphere IS NULL OR TRIM(COALESCE(u.sphere,''))='') AND COALESCE(u.can_receive_leads,1)=1) OR (LOWER(u.role)='admin' AND (u.sphere='biz' OR u.sphere IS NULL OR TRIM(COALESCE(u.sphere,''))='') AND COALESCE(u.can_receive_leads,0)=1)"
        lead_cond = BIZ_LEAD_COND
    row = execute_query(
        f"""SELECT u.user_id FROM users u
            WHERE ({role_cond})
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
    closing_pain = State()      # боль клиента (для записи в Google Sheet)
    closing_comment = State()
    closing_callback = State()  # перезвон (для записи в Google Sheet)
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
    # Маркетинг: статистика (кастомный период)
    marketing_stats_custom = State()
    # Админ бизнеса: статистика за период (диапазон дат)
    admin_biz_stats_custom = State()
    # Медицина: поступления лидов (кастомная дата/период)
    med_leads_custom_period = State()
    # Медицина: завершение диалога -> Оплатил -> выбор пакета и суммы
    med_paid_sum = State()
    # Удобство: поиск лида (ввод номера или ФИО)
    lead_search_query = State()
    # Медицина: комментарий при отказе
    refuse_comment_med = State()
    # Задача по лиду (текст и срок)
    lead_task_text = State()
    lead_task_due = State()
    # Заметка по лиду
    lead_note_text = State()
    # Выгрузка за период
    export_biz_period = State()

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
    """Отправить сообщение в WA или в Telegram-аккаунт лидов (если phone = tg_<user_id>)."""
    if str(phone).startswith("tg_"):
        try:
            peer_id = int(str(phone)[3:].strip().split("_")[0] or "0")
        except (ValueError, IndexError):
            return
        if _tg_leads and _tg_leads.is_available():
            if m.text:
                await _tg_leads.send_message_to_lead(peer_id, m.text)
            elif m.photo:
                try:
                    f = await bot.get_file(m.photo[-1].file_id)
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        await bot.download_file(f.file_path, tmp.name)
                        await _tg_leads.send_media_to_lead(peer_id, tmp.name, caption=m.caption, is_photo=True)
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                except Exception as e:
                    logging.warning("tg_leads send photo: %s", e)
            elif m.voice:
                try:
                    f = await bot.get_file(m.voice.file_id)
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                        await bot.download_file(f.file_path, tmp.name)
                        await _tg_leads.send_media_to_lead(peer_id, tmp.name, caption=m.caption, is_photo=False)
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                except Exception as e:
                    logging.warning("tg_leads send voice: %s", e)
            elif m.video:
                try:
                    f = await bot.get_file(m.video.file_id)
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                        await bot.download_file(f.file_path, tmp.name)
                        await _tg_leads.send_media_to_lead(peer_id, tmp.name, caption=m.caption, is_photo=False)
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                except Exception as e:
                    logging.warning("tg_leads send video: %s", e)
            elif m.document:
                try:
                    f = await bot.get_file(m.document.file_id)
                    ext = os.path.splitext(m.document.file_name or "")[1] or ".bin"
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        await bot.download_file(f.file_path, tmp.name)
                        await _tg_leads.send_media_to_lead(peer_id, tmp.name, caption=m.caption, is_photo=False)
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                except Exception as e:
                    logging.warning("tg_leads send document: %s", e)
            else:
                await _tg_leads.send_message_to_lead(peer_id, "[медиа]")
        return
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
    if role == 'marketing':
        kb.button(text="📈 Статистика")
        kb.adjust(1)
        return kb.as_markup(resize_keyboard=True)
    if role == 'admin' and sphere == 'med':
        kb.button(text="📊 Нагруженность"); kb.button(text="📈 Статистика лидов")
        kb.button(text="📅 Записать к Ганчине"); kb.button(text="📋 Лиды в работе"); kb.button(text="📥 Поступления лидов (мед)")
        kb.button(text="💬 Начать диалог"); kb.button(text="🔍 Поиск лида"); kb.button(text="📌 Доработать"); kb.button(text="💰 Оплаты"); kb.button(text="🔄 Продлить курс")
        kb.button(text="🎯 План/KPI")
        kb.button(text="◀ Назад в меню")
        kb.adjust(2)
        return kb.as_markup(resize_keyboard=True)
    if role == 'manager' and sphere == 'med':
        kb.button(text="👥 Мои Пациенты"); kb.button(text="📋 Мои записи"); kb.button(text="💰 Мои оплаты"); kb.button(text="⏳ Дожим"); kb.button(text="📌 Доработать")
        kb.adjust(2)
        return kb.as_markup(resize_keyboard=True)
    # Бизнес: admin — свои кнопки
    if role == 'admin' and (sphere == 'biz' or sphere is None):
        kb.button(text="📈 Статистика"); kb.button(text="👤 KPI Менеджеров")
        kb.button(text="🎯 Назначить План"); kb.button(text="📥 ВКЛ/ВЫКЛ лидов")
        kb.button(text="📋 Лиды в работе"); kb.button(text="💬 Начать диалог")
        kb.button(text="🔍 Поиск лида"); kb.button(text="📌 Доработать")
    elif role == 'manager' and (sphere == 'biz' or sphere is None):
        kb.button(text="👥 Мои клиенты"); kb.button(text="⏳ Дожим"); kb.button(text="✅ Оплачено"); kb.button(text="❌ Отказ"); kb.button(text="📌 Доработать")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def get_owner_med_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📊 Нагруженность"); kb.button(text="📈 Статистика лидов")
    kb.button(text="👤 Назначить Админа"); kb.button(text="📂 Загрузка данных")
    kb.button(text="👑 Дать права владельца"); kb.button(text="💰 Приход"); kb.button(text="🎯 План/KPI"); kb.button(text="🎯 Поставить План")
    kb.button(text="📋 Лиды в работе"); kb.button(text="📥 Поступления лидов (мед)"); kb.button(text="💬 Начать диалог")
    kb.button(text="🔍 Поиск лида"); kb.button(text="📂 Выгрузка (мед)"); kb.button(text="📌 Доработать"); kb.button(text="🔥 Уволить"); kb.button(text="⏱ Время в работе"); kb.button(text="◀ Назад")
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
    kb.button(text="📋 Распределить лиды"); kb.button(text="📥 Вернуть лиды в очередь")
    kb.button(text="📌 Доработать"); kb.button(text="👤 Назначить админа бизнеса"); kb.button(text="👑 Дать права владельца")
    kb.button(text="📂 Загрузка данных"); kb.button(text="⏱ Время в работе"); kb.button(text="🔥 Уволить"); kb.button(text="🎯 Поставить План")
    kb.button(text="📋 Создать тестовый лид")
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

def get_lead_phone_by_prefix(phone_prefix: str):
    """Вернуть полный phone из leads при точном совпадении или единственном совпадении по префиксу (для callback_data до 30 символов)."""
    if not phone_prefix:
        return None
    row = execute_query("SELECT phone FROM leads WHERE phone = ?", (phone_prefix,), fetchone=True)
    if row:
        return row[0]
    if phone_prefix.isdigit():
        row = execute_query("SELECT phone FROM leads WHERE phone = ?", (f"tg_{phone_prefix}",), fetchone=True)
        if row:
            return row[0]
    row = execute_query(
        "SELECT phone FROM leads WHERE length(phone) >= ? AND substr(phone, 1, ?) = ? LIMIT 1",
        (len(phone_prefix), len(phone_prefix), phone_prefix),
        fetchone=True,
    )
    return row[0] if row else None

def _tel_url(phone_val):
    """Не используется: Telegram отклоняет любые tel: в inline-кнопках. Используем callback show_tel_."""
    digits = "".join(c for c in str(phone_val) if c.isdigit())
    return f"tel:{digits}" if digits else "tel:"

@dp.callback_query(F.data.startswith("show_tel_"))
async def show_tel_callback(c: types.CallbackQuery):
    """По нажатию «📞 Набрать» — отправляем номер текстом (без url=tel:, т.к. Telegram его отклоняет)."""
    prefix = c.data.replace("show_tel_", "", 1).strip()
    if len(prefix) > 30:
        prefix = prefix[:30]
    full_phone = get_lead_phone_by_prefix(prefix) or prefix
    digits = "".join(ch for ch in str(full_phone) if ch.isdigit())
    if digits:
        await c.message.answer(f"📞 Набрать: +{digits}")
    await c.answer()

def lead_note_add(phone: str, user_id: int, text: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    execute_query("INSERT INTO lead_notes (phone, user_id, text, created_at) VALUES (?, ?, ?, ?)", (phone, user_id, (text or "")[:1000], now))

def lead_notes_list(phone: str, limit: int = 5):
    rows = execute_query("SELECT user_id, text, created_at FROM lead_notes WHERE phone = ? ORDER BY id DESC LIMIT ?", (phone, limit), fetchall=True)
    return rows or []

def lead_task_add(phone: str, user_id: int, text: str, due_at: str):
    execute_query("INSERT INTO lead_tasks (phone, user_id, text, due_at, done) VALUES (?, ?, ?, ?, 0)", (phone, user_id, (text or "")[:500], due_at))

def lead_tasks_list(phone: str, done_only: bool = False):
    cond = "phone = ?" + (" AND done = 1" if done_only else " AND done = 0")
    rows = execute_query(f"SELECT id, text, due_at FROM lead_tasks WHERE {cond} ORDER BY due_at ASC", (phone,), fetchall=True)
    return rows or []

def lead_tasks_due_soon(minutes: int = 15):
    """Задачи, у которых due_at через <= minutes минут (напоминание)."""
    now = datetime.now()
    end = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
    now_str = now.strftime("%Y-%m-%d %H:%M")
    rows = execute_query(
        "SELECT id, phone, user_id, text, due_at FROM lead_tasks WHERE done = 0 AND due_at IS NOT NULL AND due_at <= ? AND due_at >= ? ORDER BY due_at",
        (end, now_str), fetchall=True)
    return rows or []

def lead_task_mark_done(task_id: int):
    execute_query("UPDATE lead_tasks SET done = 1 WHERE id = ?", (task_id,))

def build_lead_card(phone: str, can_reassign: bool = False) -> tuple:
    """Возвращает (текст карточки, InlineKeyboardMarkup). can_reassign — показывать кнопку Переназначить."""
    row = execute_query(
        "SELECT name, status, manager_id, touches, last_touch, direction, COALESCE(source,'—') FROM leads WHERE phone = ?",
        (phone,),
        fetchone=True,
    )
    if not row:
        return "Лид не найден.", None
    name, status, mgr_id, touches, last_touch, direction, source = row[0], row[1], row[2], row[3], row[4], (row[5] or "biz"), (row[6] if len(row) > 6 else "—")
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
        f"🏷 Источник: {source}\n"
        f"👔 Менеджер: {mgr_fio}\n"
        f"📊 Касания: {touches or 0} · Последний контакт: {lt}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать", callback_data=f"wlead_{phone[:30]}")
    kb.button(text="📞 Позвонить", callback_data=f"call_{phone[:30]}")
    kb.button(text="📜 История", callback_data=f"hist_{phone[:30]}")
    kb.button(text="📝 Заметка", callback_data=f"note_{phone[:30]}")
    kb.button(text="⏰ Задача", callback_data=f"task_{phone[:30]}")
    kb.button(text="📋 Задачи", callback_data=f"tasks_{phone[:30]}")
    kb.button(text="🏷 Источник", callback_data=f"src_{phone[:30]}")
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
    """
    Вытащить текст из входящего сообщения WA.
    Учитываем обычный текст, расширенный текст (ответ на сообщение) и файлы.
    """
    md = body.get('messageData') or {}
    # Обычное текстовое сообщение
    tmd = md.get('textMessageData')
    if tmd and isinstance(tmd, dict):
        txt = tmd.get('textMessage')
        if txt:
            return txt
    # Расширенный текст (ответ/цитата сообщения, extendedTextMessageData)
    ext = md.get('extendedTextMessageData')
    if ext and isinstance(ext, dict):
        txt = ext.get('text') or ext.get('description') or ext.get('caption')
        if txt:
            return txt
    # Сообщение с файлом без отдельного текста
    if md.get('fileMessageData'):
        return '[Файл/голос]'
    return '...'

def _get_wa_file_info(body):
    """Если входящее сообщение — файл (фото/видео/аудио/документ), возвращает (typeMessage, downloadUrl, caption). Иначе None."""
    md = body.get('messageData') or {}
    t = md.get('typeMessage')
    fmd = md.get('fileMessageData') or body.get('fileMessageData') or {}
    if not t and fmd:
        t = body.get('typeMessage') or 'imageMessage'
    url = fmd.get('downloadUrl') or body.get('downloadUrl')
    if not url:
        return None
    if not t:
        t = 'imageMessage'
    if t not in ('imageMessage', 'videoMessage', 'audioMessage', 'documentMessage'):
        t = 'imageMessage'
    caption = (fmd.get('caption') or body.get('caption') or '').strip() or None
    return (t, url, caption)

async def _send_wa_text(api_url, token, chat_id, text):
    url = f"{api_url}/sendMessage/{token}"
    await _wa_post(url, {"chatId": chat_id, "message": text})

# Текст напоминания тем, кому не ответили больше 24 ч (таджикский)
REMINDER_24H_TEXT = "Салом, узр хохиш зиёд барои шуморо бе чавоб мондан, хохиш мекунем якбори дигар саволатонро такрор кунед!"

# --- МОНИТОРИНГ: два инстанса параллельно ---
async def _process_wa_instance(instance_name, base_url, instance_id, token):
    """Обработка одного инстанса WA. instance_name = 'biz' | 'med'."""
    receive_url = f"{base_url}/waInstance{instance_id}/receiveNotification/{token}"
    delete_url = f"{base_url}/waInstance{instance_id}/deleteNotification/{token}"
    send_msg_url = f"{base_url}/waInstance{instance_id}/sendMessage/{token}"
    chat_prefix = ""
    med_log_interval = 0  # счётчик для периодического лога "MED: polling"
    biz_log_interval = 0  # счётчик для периодического лога "BIZ: polling"
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
                    biz_log_interval += 1
                    if status != 200:
                        try:
                            err_body = text[:500] if text else ""
                        except Exception:
                            err_body = ""
                        logging.warning("BIZ: receiveNotification вернул HTTP %s — %s", status, err_body)
                        print(f"[CRM] BIZ: HTTP {status} — {err_body}")
                        break
                    if not j:
                        if biz_log_interval % 30 == 1:
                            print(f"[CRM] BIZ: опрос пустой (инстанс {instance_id}), ждём сообщений...")
                        break
                d = j
                rid = d.get('receiptId')
                body = d.get('body', {})
                tw = body.get('typeWebhook')
                if tw not in ('incomingMessageReceived', 'incomingFileMessageReceived'):
                    logging.info("%s: тип события %s (пропускаем)", instance_name.upper(), tw)
                    print(f"[CRM] {instance_name.upper()}: typeWebhook={tw!r} — пропуск")
                    if rid:
                        await _wa_delete(f"{base_url}/waInstance{instance_id}/deleteNotification/{token}/{rid}")
                    continue
                raw_phone = body.get('senderData', {}).get('chatId', '').split('@')[0]
                phone = ''.join(c for c in raw_phone if c.isdigit()) or raw_phone
                if not phone:
                    if rid:
                        await _wa_delete(f"{base_url}/waInstance{instance_id}/deleteNotification/{token}/{rid}")
                    continue
                logging.info("%s: получено сообщение от %s (typeWebhook=%s)", instance_name.upper(), phone, tw)
                print(f"[CRM] {instance_name.upper()}: получено сообщение от {phone}")
                chat_id = f"{phone}@c.us"
                exist = execute_query("SELECT manager_id, name, status, direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
                if exist:
                    mgr_id, c_name, status = exist[0], exist[1], exist[2]
                    lead_dir = (exist[3] or 'biz') if len(exist) > 3 else 'biz'
                    if lead_dir != instance_name:
                        logging.info("%s: сообщение от %s проигнорировано (лид в базе как direction=%s)", instance_name.upper(), phone, lead_dir)
                        print(f"[CRM] {instance_name.upper()}: сообщение от {phone} проигнорировано (лид уже в базе как {lead_dir})")
                        await _wa_delete(delete_url + "/" + str(rid))
                        continue
                    execute_query("UPDATE leads SET last_touch = ? WHERE phone = ?", (datetime.now().strftime("%Y-%m-%d %H:%M"), phone))
                    is_active = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (mgr_id,), fetchone=True)
                    if not is_active or is_active[1] != instance_name:
                        # Менеджера нет или сфера не та — пробуем назначить свободного
                        if mgr_id:
                            execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (mgr_id, phone))
                        target = get_free_manager_for_direction(instance_name)
                        if target:
                            execute_query("UPDATE users SET is_busy=1 WHERE user_id=?", (target,))
                            execute_query("UPDATE leads SET manager_id = ?, status = 'active' WHERE phone = ?", (target, phone))
                            mgr_id = target
                            logging.info("%s: лид %s переназначен свободному менеджеру %s", instance_name.upper(), phone, target)
                            print(f"[CRM] {instance_name.upper()}: лид {phone} переназначен менеджеру {target}")
                        else:
                            execute_query("UPDATE leads SET manager_id = NULL, status = 'pending' WHERE phone = ?", (phone,))
                            await _wa_delete(delete_url + "/" + str(rid))
                            processed_this_cycle += 1
                            continue
                    # Если менеджер сейчас в диалоге с ДРУГИМ клиентом — этот лид не перебиваем: в очередь, сообщение не пересылаем.
                    session = execute_query("SELECT phone FROM chat_sessions WHERE user_id = ?", (mgr_id,), fetchone=True)
                    if session and session[0] != phone:
                        execute_query("UPDATE leads SET manager_id = NULL, status = 'pending' WHERE phone = ?", (phone,))
                        execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (mgr_id, phone))
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
                        kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
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
                    # Нет лида в базе — новый контакт (логика одинакова для медицины и бизнеса)
                    q = execute_query("SELECT step, instance_sphere FROM auth_queue WHERE phone = ?", (phone,), fetchone=True)
                    if not q:
                        # Первый контакт — просим имя (med и biz)
                        logging.info("%s: первый контакт с %s, просим имя", instance_name.upper(), phone)
                        print(f"[CRM] {instance_name.upper()}: первый контакт с {phone}, просим имя")
                        msg = "Салом, ном ва насаби худро нависед! Мо дар муддати кутоҳтарин ба шумо ҷавоб медиҳем!"
                        await _wa_post(send_msg_url, {"chatId": chat_id, "message": msg})
                        execute_query("INSERT OR REPLACE INTO auth_queue (phone, step, instance_sphere) VALUES (?, 1, ?)", (phone, instance_name))
                    elif q[0] == 1 and (q[1] or instance_name) == instance_name:
                        # Второе сообщение (имя) — создаём лид (med и biz)
                        logging.info("%s: второе сообщение (имя) от %s, создаём лид", instance_name.upper(), phone)
                        print(f"[CRM] {instance_name.upper()}: второе сообщение (имя) от {phone}, создаём лид")
                        c_name = _extract_wa_text(body)
                        if c_name == '[Файл/голос]':
                            c_name = 'Клиент'
                        execute_query("DELETE FROM auth_queue WHERE phone = ?", (phone,))
                        target = get_free_manager_for_direction(instance_name)
                        if target:
                            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            execute_query("UPDATE users SET is_busy=1 WHERE user_id=?", (target,))
                            execute_query(
                                "INSERT INTO leads (phone, name, status, manager_id, last_touch, touches, direction, created_at, source) VALUES (?, ?, 'active', ?, ?, 1, ?, ?, 'WhatsApp')",
                                (phone, c_name, target, now_iso, instance_name, now_iso),
                            )
                            kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
                            logging.info("%s: new lead %s -> manager %s", instance_name, phone, target)
                            print(f"[CRM] {instance_name}: new lead {phone} -> manager {target}")
                            try:
                                await bot.send_message(target, f"📥 <b>НОВЫЙ ЛИД</b>\n👤 {c_name}\n📞 {phone}", reply_markup=kb, parse_mode="HTML")
                            except Exception as e:
                                logging.exception("send_message to manager %s failed: %s", target, e)
                                print(f"[CRM] Ошибка отправки карточки менеджеру {target}: {e}")
                        else:
                            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            execute_query(
                                "INSERT INTO leads (phone, name, status, manager_id, last_touch, touches, direction, created_at, source) VALUES (?, ?, 'pending', NULL, ?, 1, ?, ?, 'WhatsApp')",
                                (phone, c_name, now_iso, instance_name, now_iso),
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

async def _on_telegram_lead_message(peer_id: int, name: str, text: str, has_media: bool):
    """Входящие с Telegram-аккаунта лидов — только направление БИЗНЕС: создать лид или переслать менеджеру."""
    phone = f"tg_{peer_id}"
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = execute_query("SELECT manager_id FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not row:
        target = get_free_manager_for_direction('biz')
        if target:
            execute_query("UPDATE users SET is_busy=1 WHERE user_id=?", (target,))
            execute_query(
                "INSERT INTO leads (phone, name, status, manager_id, last_touch, touches, direction, created_at, source) VALUES (?, ?, 'active', ?, ?, 1, 'biz', ?, 'Telegram')",
                (phone, name, target, now_iso, now_iso),
            )
            kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
            try:
                await bot.send_message(target, f"📥 <b>НОВЫЙ ЛИД (Telegram)</b>\n👤 {name}\n🆔 {peer_id}", reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logging.exception("tg_lead send to manager %s: %s", target, e)
            logging.info("tg_lead: new lead %s -> manager %s", phone, target)
        else:
            execute_query(
                "INSERT INTO leads (phone, name, status, manager_id, last_touch, touches, direction, created_at, source) VALUES (?, ?, 'pending', NULL, ?, 1, 'biz', ?, 'Telegram')",
                (phone, name, now_iso, now_iso),
            )
            logging.warning("tg_lead: all busy, lead %s in queue", phone)
    else:
        mgr_id = row[0]
        kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
        try:
            await bot.send_message(mgr_id, f"💬 <b>{name}</b> (Telegram):\n{text}", reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logging.warning("tg_lead forward to manager %s: %s", mgr_id, e)
    log_lead_event(phone, "incoming", (text or "[медиа]")[:200], None)
    execute_query("UPDATE leads SET last_touch = ? WHERE phone = ?", (now_iso, phone))

async def _wa_send_reminder(instance_name: str):
    """Отправить напоминание (таджикский текст) в WA тем, кому не отвечали 24+ ч."""
    if instance_name == 'med':
        base_url, iid, token = MED_API_URL, MED_ID_INSTANCE, MED_API_TOKEN
        cond = "direction = 'med'"
    else:
        base_url, iid, token = API_URL, ID_INSTANCE, API_TOKEN_INSTANCE
        cond = BIZ_LEAD_COND
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
            await asyncio.sleep(300)
            cutoff = (datetime.now() - timedelta(minutes=CHATTING_IDLE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            rows = execute_query(
                "SELECT user_id, phone, reminder_sent FROM chat_sessions WHERE last_outgoing_at < ? AND COALESCE(reminder_sent, 0) = 0",
                (cutoff,),
                fetchall=True,
            )
            for (uid, phone, _) in (rows or []):
                if is_owner(uid):
                    continue
                try:
                    await bot.send_message(uid, "⚠️ Вы не отвечаете клиенту! Завершите диалог, если закончили.")
                    execute_query("UPDATE chat_sessions SET reminder_sent = 1 WHERE user_id = ? AND phone = ?", (uid, phone))
                except Exception as e:
                    logging.warning("chatting_idle reminder to %s: %s", uid, e)
        except Exception as e:
            logging.exception("job_chatting_idle: %s", e)

async def job_tasks_reminder():
    """Напоминания по задачам: задачи с due_at в ближайшие 15 минут — отправить уведомление менеджеру."""
    while True:
        try:
            await asyncio.sleep(120)
            rows = lead_tasks_due_soon(15)
            for (tid, phone, user_id, text, due_at) in (rows or []):
                try:
                    row = execute_query("SELECT name FROM leads WHERE phone = ?", (phone,), fetchone=True)
                    name = (row[0] if row else phone)
                    await bot.send_message(user_id, f"⏰ <b>Напоминание</b>\nЛид: {name} ({phone})\nЗадача: {text}\nСрок: {due_at}", parse_mode="HTML")
                    lead_task_mark_done(tid)
                except Exception as e:
                    logging.warning("task_reminder to %s: %s", user_id, e)
        except Exception as e:
            logging.exception("job_tasks_reminder: %s", e)

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

@dp.callback_query(F.data.startswith("cl_") | F.data.startswith("f_s_") | F.data.startswith("f_t_") | F.data.startswith("f_r_") | F.data.startswith("f_n_"))
async def closing(c: types.CallbackQuery, state: FSMContext):
    if c.data.startswith("cl_"):
        p = c.data[3:].strip()
    else:
        p = c.data.split("_", 2)[-1].strip() if "_" in c.data else ""
    full_phone = get_lead_phone_by_prefix(p) or p
    phone_for_tel = "+" + "".join(c for c in str(full_phone) if c.isdigit())
    if c.data.startswith("cl_"):
        direction = get_lead_direction(full_phone)
        if direction == 'med':
            kb = InlineKeyboardBuilder()
            kb.button(text=f"📞 Набрать {phone_for_tel}", callback_data=f"show_tel_{p}")
            kb.button(text="❌ Отказ", callback_data=f"med_r_{p}")
            kb.button(text="⏳ Подумает", callback_data=f"med_t_{p}")
            kb.button(text="💰 Оплатил", callback_data=f"med_p_{p}")
            kb.button(text="🚫 НЕ ОТВЕЧАЮТ", callback_data=f"med_n_{p}")
            kb.adjust(1)
            await c.message.answer("Итог звонка (нажмите «Набрать» — откроется набор номера в телефоне):", reply_markup=kb.as_markup())
        else:
            kb = InlineKeyboardBuilder()
            kb.button(text=f"📞 Набрать {phone_for_tel}", callback_data=f"show_tel_{p}")
            kb.button(text="💰 ОПЛАТИЛ", callback_data=f"f_s_{p}")
            kb.button(text="⏳ ДУМАЕТ", callback_data=f"f_t_{p}")
            kb.button(text="❌ ОТКАЗ", callback_data=f"f_r_{p}")
            kb.button(text="🚫 НЕ ОТВЕТИЛ", callback_data=f"f_n_{p}")
            kb.adjust(1)
            await c.message.answer("Итог звонка (нажмите «Набрать» — откроется набор номера в телефоне):", reply_markup=kb.as_markup())
    else:
        # f_s_, f_t_, f_r_, f_n_ — итоги по бизнесу
        res = c.data.split("_")[1]
        if res == 'n':
            l_data = execute_query("SELECT name FROM leads WHERE phone = ?", (full_phone,), fetchone=True)
            if not l_data:
                await c.answer("Лид не найден.")
                return
            lead_name = l_data[0] or full_phone
            execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (c.from_user.id, full_phone))
            execute_query("UPDATE leads SET status='thinking', last_touch=? WHERE phone=?", (datetime.now(), full_phone))
            execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
            u = execute_query("SELECT sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
            mgr_sphere = (u[0] or 'biz') if u else 'biz'
            await try_assign_queued_lead_to_manager(c.from_user.id, mgr_sphere)
            try:
                await c.message.delete()
            except Exception:
                pass
            await c.message.answer(f"🔄 Лид {lead_name} в дожиме. Откройте «⏳ Дожим» для списка.", reply_markup=get_main_menu(c.from_user.id))
        else:
            await state.update_data(c_phone=full_phone, c_status=res)
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

@dp.message(F.text == "📋 Создать тестовый лид")
async def owner_create_test_lead(m: types.Message, state: FSMContext):
    """Владелец: создать тестовый лид, назначить себе и отправить карточку с кнопками менеджера (Итог звонка) для проверки процесса."""
    if not is_owner(m.from_user.id):
        return
    await state.update_data(owner_current_sphere='biz')
    owner_id = m.from_user.id
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    phone = f"TEST_{owner_id}_{int(datetime.now().timestamp())}"
    execute_query(
        """INSERT OR REPLACE INTO leads (phone, name, status, manager_id, direction, created_at, touches)
           VALUES (?, ?, 'active', ?, 'biz', ?, 0)""",
        (phone, "Тестовый лид (директор)", owner_id, now_iso),
    )
    execute_query(
        "INSERT OR REPLACE INTO chat_sessions (user_id, phone, last_outgoing_at, reminder_sent) VALUES (?, ?, ?, 0)",
        (owner_id, phone, now_iso),
    )
    execute_query("UPDATE users SET is_busy = 1 WHERE user_id = ?", (owner_id,))
    log_lead_event(phone, "status_change", "Создан тестовый лид (владелец)", owner_id)
    text = (
        f"👤 <b>Тестовый лид (директор)</b>\n"
        f"📞 {phone}\n"
        f"📂 Бизнес · Статус: active\n"
        f"👔 Менеджер: вы\n\n"
        f"Проверка процесса: нажмите «📋 Итог звонка» и пройдите этапы закрытия (Сфера → Боль клиента → Комментарий → Перезвон). После закрытия вернётся меню владельца."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}")
    kb.button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}")
    kb.button(text="📋 Итог звонка", callback_data=f"cl_{phone[:30]}")
    kb.adjust(2, 1)
    await m.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

@dp.message(F.text == "◀ Назад")
@dp.message(F.text == "◀ Назад в меню")
async def back_main(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("Главное меню", reply_markup=get_main_menu(m.from_user.id))

# --- Бизнес: статистика (владелец — общая воронка; админ — за период) ---
def _biz_leads_cond():
    return " " + BIZ_LEAD_COND + " "

def _biz_stats_for_period(start_dt, end_dt):
    """Вернуть (total, answered, sold) за период по created_at для бизнес-лидов."""
    cond = _biz_leads_cond()
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond.strip()}{NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    answered = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond.strip()}{NOT_TEST_LEAD_COND} AND is_answered = 1 AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    sold = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond.strip()} AND status = 'closed' AND is_sale = 1 AND (comment IS NULL OR comment NOT LIKE '%Автозакрытие%'){NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    return total, answered, sold

def _med_stats_for_period(start_dt, end_dt):
    """Вернуть (total, answered, sold) за период по created_at для мед-лидов."""
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    cond = "direction = 'med'"
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    answered = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND is_answered = 1 AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    sold = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond} AND status = 'closed' AND is_answered = 1 AND (comment IS NULL OR comment NOT LIKE '%Автозакрытие%'){NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    return total, answered, sold

@dp.message(F.text == "📈 Статистика")
async def stats(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, sphere = u[0], u[1]
    # Маркетинг: выбор направления (медицина / бизнес)
    if role == 'marketing':
        kb = InlineKeyboardBuilder()
        kb.button(text="🏥 Медицина", callback_data="mktstat_dir_med")
        kb.button(text="💼 Бизнес", callback_data="mktstat_dir_biz")
        kb.adjust(2)
        await m.answer("Выберите направление для статистики:", reply_markup=kb.as_markup())
        return
    # Админ бизнеса: сначала выбор периода по бизнесу
    if role == 'admin' and (sphere == 'biz' or sphere is None):
        kb = InlineKeyboardBuilder()
        kb.button(text="Сегодня", callback_data="admstat_today")
        kb.button(text="Вчера", callback_data="admstat_yesterday")
        kb.button(text="Другая дата (диапазон)", callback_data="admstat_custom")
        kb.adjust(1)
        await m.answer("За какой период показать статистику (Бизнес)?", reply_markup=kb.as_markup())
        return
    # Владелец или общая: воронка по бизнесу без периода
    await chat_history_delete_messages(m.from_user.id, context="kpi")
    cond = _biz_leads_cond()
    all_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE 1=1 AND {cond.strip()}{NOT_TEST_LEAD_COND}", fetchone=True)[0] or 1
    ans_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE is_answered=1 AND {cond.strip()}{NOT_TEST_LEAD_COND}", fetchone=True)[0]
    sold_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE status='closed' AND (comment IS NULL OR comment NOT LIKE '%Автозакрытие%') AND is_sale=1 AND {cond.strip()}{NOT_TEST_LEAD_COND}", fetchone=True)[0]
    c_ans = round((ans_l / all_l) * 100, 1)
    c_sale = round((sold_l / (ans_l or 1)) * 100, 1)
    msg = await m.answer(
        "📊 <b>ОБЩАЯ ВОРОНКА (Бизнес)</b>\n\n"
        f"📥 Лидов: {all_l}\n"
        f"📞 Дозвоны: {ans_l} ({c_ans}%)\n"
        f"💰 Продажи: {sold_l} ({c_sale}% из дозвонов)",
        parse_mode="HTML",
    )
    chat_history_add(m.from_user.id, msg.message_id, context="stats")

@dp.callback_query(F.data.startswith("admstat_"))
async def admin_biz_stats_cb(c: types.CallbackQuery, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not u or u[0] != 'admin' or (u[1] != 'biz' and u[1] is not None):
        await c.answer()
        return
    period = c.data.replace("admstat_", "")
    now = datetime.now()
    if period == "custom":
        await state.set_state(Form.admin_biz_stats_custom)
        await c.message.edit_text("Введите диапазон дат:\nПример: 2026-02-01 2026-02-26")
        await c.answer()
        return
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        label = "сегодня"
    else:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        label = "вчера"
    total, answered, sold = _biz_stats_for_period(start, end)
    not_answered = total - answered
    conv_contact = round((answered / (total or 1)) * 100, 1)
    conv_sale = round((sold / (answered or 1)) * 100, 1)
    text = (
        f"📊 <b>Статистика (Бизнес) — {label}</b>\n\n"
        f"📥 Поступление лидов: {total}\n"
        f"❌ Не ответивших: {not_answered}\n"
        f"📞 Конверсия на дозвон: {conv_contact}% ({answered} из {total})\n"
        f"💰 Продажи: {sold}\n"
        f"📈 Конверсия в продажу (из дозвонов): {conv_sale}%"
    )
    await c.message.edit_text(text, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("mktstat_dir_"))
async def marketing_stats_choose_direction(c: types.CallbackQuery, state: FSMContext):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not u or u[0] != 'marketing':
        await c.answer()
        return
    direction = c.data.replace("mktstat_dir_", "")
    if direction not in ("med", "biz"):
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data=f"mktstat_{direction}_today")
    kb.button(text="Вчера", callback_data=f"mktstat_{direction}_yesterday")
    kb.button(text="Период", callback_data=f"mktstat_{direction}_custom")
    kb.adjust(1)
    title = "Медицина" if direction == "med" else "Бизнес"
    await c.message.edit_text(f"Статистика ({title}). Выберите период:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("mktstat_"))
async def marketing_stats_period_cb(c: types.CallbackQuery, state: FSMContext):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not u or u[0] != 'marketing':
        await c.answer()
        return
    parts = c.data.split("_", 2)
    if len(parts) < 3:
        await c.answer()
        return
    _, direction, period = parts
    now = datetime.now()
    if period == "custom":
        await state.set_state(Form.marketing_stats_custom)
        await state.update_data(mkt_direction=direction)
        title = "Медицина" if direction == "med" else "Бизнес"
        await c.message.edit_text(
            f"Статистика ({title}). Введите диапазон дат:\nПример: 2026-02-01 2026-02-26"
        )
        await c.answer()
        return
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        label = "сегодня"
    else:  # yesterday
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        label = "вчера"
    if direction == "biz":
        total, answered, sold = _biz_stats_for_period(start, end)
        title = "Бизнес"
    else:
        total, answered, sold = _med_stats_for_period(start, end)
        title = "Медицина"
    not_answered = total - answered
    conv_contact = round((answered / (total or 1)) * 100, 1)
    conv_sale = round((sold / (answered or 1)) * 100, 1)
    text = (
        f"📊 <b>Статистика ({title}) — {label}</b>\n\n"
        f"📥 Поступление лидов: {total}\n"
        f"❌ Не ответивших: {not_answered}\n"
        f"📞 Конверсия на дозвон: {conv_contact}% ({answered} из {total})\n"
        f"💰 Продажи: {sold}\n"
        f"📈 Конверсия в продажу (из дозвонов): {conv_sale}%"
    )
    await c.message.edit_text(text, parse_mode="HTML")
    await c.answer()

@dp.message(Form.marketing_stats_custom)
async def marketing_stats_custom_done(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'marketing':
        await state.clear()
        return
    data = await state.get_data()
    direction = data.get("mkt_direction") or "biz"
    parts = (m.text or "").strip().split()
    try:
        if len(parts) == 1:
            d1 = datetime.strptime(parts[0], "%Y-%m-%d").date()
            d2 = d1
        elif len(parts) == 2:
            d1 = datetime.strptime(parts[0], "%Y-%m-%d").date()
            d2 = datetime.strptime(parts[1], "%Y-%m-%d").date()
        else:
            raise ValueError()
        if d1 > d2:
            d1, d2 = d2, d1
        start = datetime.combine(d1, datetime.min.time())
        end = datetime.combine(d2, datetime.max.time())
    except ValueError:
        await m.answer("Неверный формат. Пример:\n2026-02-24\nили\n2026-02-01 2026-02-26")
        return
    if direction == "biz":
        total, answered, sold = _biz_stats_for_period(start, end)
        title = "Бизнес"
    else:
        total, answered, sold = _med_stats_for_period(start, end)
        title = "Медицина"
    not_answered = total - answered
    conv_contact = round((answered / (total or 1)) * 100, 1)
    conv_sale = round((sold / (answered or 1)) * 100, 1)
    label = f"{d1} — {d2}" if d1 != d2 else str(d1)
    text = (
        f"📊 <b>Статистика ({title}) — {label}</b>\n\n"
        f"📥 Поступление лидов: {total}\n"
        f"❌ Не ответивших: {not_answered}\n"
        f"📞 Конверсия на дозвон: {conv_contact}% ({answered} из {total})\n"
        f"💰 Продажи: {sold}\n"
        f"📈 Конверсия в продажу (из дозвонов): {conv_sale}%"
    )
    await m.answer(text, parse_mode="HTML")
    await state.clear()
@dp.message(Form.admin_biz_stats_custom)
async def admin_biz_stats_custom_done(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'admin' or (u[1] != 'biz' and u[1] is not None):
        await state.clear()
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
        if d1 > d2:
            d1, d2 = d2, d1
        start = datetime.combine(d1, datetime.min.time())
        end = datetime.combine(d2, datetime.max.time())
    except ValueError:
        await m.answer("Неверный формат. Пример:\n2026-02-24\nили\n2026-02-01 2026-02-26")
        return
    total, answered, sold = _biz_stats_for_period(start, end)
    not_answered = total - answered
    conv_contact = round((answered / (total or 1)) * 100, 1)
    conv_sale = round((sold / (answered or 1)) * 100, 1)
    label = f"{d1} — {d2}" if d1 != d2 else str(d1)
    text = (
        f"📊 <b>Статистика (Бизнес) — {label}</b>\n\n"
        f"📥 Поступление лидов: {total}\n"
        f"❌ Не ответивших: {not_answered}\n"
        f"📞 Конверсия на дозвон: {conv_contact}% ({answered} из {total})\n"
        f"💰 Продажи: {sold}\n"
        f"📈 Конверсия в продажу (из дозвонов): {conv_sale}%"
    )
    await m.answer(text, parse_mode="HTML")
    await state.clear()

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
        m_all = execute_query(f"SELECT COUNT(*) FROM leads WHERE manager_id=? AND {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND}", (mid,), fetchone=True)[0] or 1
        m_ans = execute_query(f"SELECT COUNT(*) FROM leads WHERE manager_id=? AND is_answered=1 AND {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND}", (mid,), fetchone=True)[0]
        m_sold = execute_query(f"SELECT COUNT(*) FROM leads WHERE manager_id=? AND status='closed' AND is_sale=1 AND (comment IS NULL OR comment NOT LIKE '%Автозакрытие%') AND {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND}", (mid,), fetchone=True)[0]
        extra = execute_query("SELECT extra_sales FROM manager_extra_sales WHERE manager_id = ?", (mid,), fetchone=True)
        m_sold += (extra[0] if extra else 0)
        m_t = execute_query(f"SELECT AVG(touches) FROM leads WHERE manager_id=? AND {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND}", (mid,), fetchone=True)[0] or 0
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
    cond = " " + BIZ_LEAD_COND + " "
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND is_answered = 1 AND created_at BETWEEN ? AND ?",
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
    cond = " " + BIZ_LEAD_COND + " "
    total = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND is_answered = 1 AND created_at BETWEEN ? AND ?",
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
    kb.button(text="📊 МАРКЕТИНГ", callback_data=f"ap_mkt_{m.from_user.id}")
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

@dp.callback_query(F.data.startswith("ap_mkt_"))
async def ap_mkt(c: types.CallbackQuery):
    uid = int(c.data.split("_")[-1])
    row = execute_query("SELECT fio FROM pending_reg WHERE user_id = ?", (uid,), fetchone=True)
    if not row:
        await c.answer("Заявка уже обработана."); return
    fio = row[0]
    execute_query("DELETE FROM pending_reg WHERE user_id = ?", (uid,))
    execute_query("INSERT INTO users (user_id, role, fio, sphere) VALUES (?, 'marketing', ?, NULL)", (uid, fio))
    await bot.send_message(uid, "🎉 Одобрено (Маркетинг)! Жми /start")
    await c.message.edit_text(f"✅ {fio} — Маркетинг.")
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
        sphere_cond = " AND " + BIZ_LEAD_COND
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
        cb = f"card_{phone[:30]}"
        kb.button(text=label, callback_data=cb)
    kb.adjust(1)
    await m.answer("Выберите лид:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("card_"))
async def lead_card_show(c: types.CallbackQuery):
    phone_prefix = c.data[5:].strip()
    if not phone_prefix:
        await c.answer()
        return
    phone = get_lead_phone_by_prefix(phone_prefix)
    if not phone:
        phone = phone_prefix
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
    full_phone = get_lead_phone_by_prefix(phone) or phone
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    await state.update_data(target=full_phone, new_chat_sphere=get_lead_direction(full_phone))
    await state.set_state(Form.waiting_for_reply)
    row = execute_query("SELECT name FROM leads WHERE phone = ?", (full_phone,), fetchone=True)
    name = row[0] if row else full_phone
    await c.message.answer(
        f"💬 <b>Диалог с {name}</b> ({full_phone})\n\nОтправляйте текст, голос, фото, видео — всё уйдёт в WA. По окончании нажмите «Завершить диалог».",
        reply_markup=get_med_finish_dialog_kb(),
        parse_mode="HTML",
    )
    events = execute_query("SELECT event_type, event_data, created_at FROM lead_events WHERE phone = ? ORDER BY id DESC LIMIT 8", (full_phone,), fetchall=True)
    if events:
        lines = []
        for et, data, created in reversed(events):
            lbl = "📩" if et == "incoming" else "📤" if et == "outgoing" else "🔄"
            lines.append(f"{created} {lbl} {data[:60]}..." if data and len(data) > 60 else f"{created} {lbl} {data or ''}")
        await c.message.answer("📜 Последняя переписка:\n\n" + "\n".join(lines))
    await c.answer()

@dp.callback_query(F.data.startswith("call_"))
async def lead_card_call(c: types.CallbackQuery):
    prefix = c.data[5:].strip()
    full_phone = get_lead_phone_by_prefix(prefix) or prefix
    phone_for_tel = "+" + "".join(ch for ch in str(full_phone) if ch.isdigit())
    await c.answer()
    await c.message.answer(f"📞 Позвонить: {phone_for_tel}")

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

@dp.callback_query(F.data.startswith("note_"))
async def lead_note_start(c: types.CallbackQuery, state: FSMContext):
    phone = get_lead_phone_by_prefix(c.data[5:].strip()) or c.data[5:].strip()
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    await state.update_data(note_phone=phone)
    await state.set_state(Form.lead_note_text)
    await c.message.answer("📝 Введите текст заметки к лиду:")
    await c.answer()

@dp.message(Form.lead_note_text)
async def lead_note_done(m: types.Message, state: FSMContext):
    d = await state.get_data()
    phone = d.get("note_phone")
    if not phone:
        await state.clear()
        return
    text = (m.text or "").strip() or "—"
    lead_note_add(phone, m.from_user.id, text)
    await state.clear()
    notes = lead_notes_list(phone, 3)
    msg = "✅ Заметка добавлена."
    if notes:
        msg += "\n\nПоследние заметки:\n" + "\n".join(f"{n[2]} — {n[1][:50]}" for n in notes)
    await m.answer(msg[:2000])
    text_card, kbd = build_lead_card(phone, _can_search_leads(m.from_user.id)[0])
    await m.answer(text_card, reply_markup=kbd, parse_mode="HTML")

@dp.callback_query(F.data.startswith("task_"))
async def lead_task_start(c: types.CallbackQuery, state: FSMContext):
    phone = get_lead_phone_by_prefix(c.data[5:].strip()) or c.data[5:].strip()
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    await state.update_data(task_phone=phone)
    await state.set_state(Form.lead_task_text)
    await c.message.answer("⏰ Введите текст задачи (например: Перезвонить):")
    await c.answer()

@dp.message(Form.lead_task_text)
async def lead_task_text_done(m: types.Message, state: FSMContext):
    d = await state.get_data()
    phone = d.get("task_phone")
    if not phone:
        await state.clear()
        return
    await state.update_data(task_text=(m.text or "").strip() or "Задача")
    await state.set_state(Form.lead_task_due)
    await m.answer("Введите когда напомнить (например: 2ч или 2026-02-26 14:00):")

@dp.message(Form.lead_task_due)
async def lead_task_due_done(m: types.Message, state: FSMContext):
    d = await state.get_data()
    phone = d.get("task_phone")
    text = d.get("task_text", "Задача")
    if not phone:
        await state.clear()
        return
    raw = (m.text or "").strip()
    due_at = None
    try:
        if raw.endswith("ч") or raw.endswith("ч."):
            h = int("".join(c for c in raw if c.isdigit()) or "0")
            due_at = (datetime.now() + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")
        elif raw.endswith("м") or raw.endswith("м.") or "мин" in raw:
            mins = int("".join(c for c in raw if c.isdigit()) or "0")
            due_at = (datetime.now() + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
        else:
            due_at = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
    except Exception:
        due_at = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    lead_task_add(phone, m.from_user.id, text, due_at)
    await state.clear()
    await m.answer(f"✅ Задача добавлена. Напоминание в {due_at}.")
    text_card, kbd = build_lead_card(phone, _can_search_leads(m.from_user.id)[0])
    await m.answer(text_card, reply_markup=kbd, parse_mode="HTML")

@dp.callback_query(F.data.startswith("tasks_"))
async def lead_tasks_list_cb(c: types.CallbackQuery):
    phone = get_lead_phone_by_prefix(c.data[6:].strip()) or c.data[6:].strip()
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    tasks = lead_tasks_list(phone)
    if not tasks:
        await c.answer("Нет активных задач по этому лиду.")
        return
    lines = []
    kb = InlineKeyboardBuilder()
    for tid, ttext, tdue in tasks:
        lines.append(f"⏰ {tdue} — {ttext[:40]}")
        kb.button(text=f"✓ {ttext[:25]}", callback_data=f"taskdone_{phone[:30]}_{tid}")
    kb.adjust(1)
    await c.message.answer("📋 Задачи по лиду:\n\n" + "\n".join(lines), reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("taskdone_"))
async def lead_task_done_cb(c: types.CallbackQuery):
    parts = c.data.split("_")
    if len(parts) < 3:
        await c.answer()
        return
    try:
        tid = int(parts[2])
    except ValueError:
        await c.answer()
        return
    lead_task_mark_done(tid)
    await c.message.edit_text(c.message.text + "\n\n✅ Отмечено выполненным.")
    await c.answer()

@dp.callback_query(F.data.startswith("src_"))
async def lead_source_choose(c: types.CallbackQuery):
    phone = get_lead_phone_by_prefix(c.data[4:].strip()) or c.data[4:].strip()
    if not phone:
        await c.answer()
        return
    can, _ = _can_search_leads(c.from_user.id)
    if not can:
        await c.answer("Доступ запрещён.")
        return
    kb = InlineKeyboardBuilder()
    for label, key in [("WhatsApp", "WhatsApp"), ("Сайт", "Сайт"), ("Реклама", "Реклама")]:
        kb.button(text=label, callback_data=f"srcset_{phone[:30]}_{key[:20]}")
    kb.adjust(1)
    await c.message.answer("Выберите источник лида:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("srcset_"))
async def lead_source_set(c: types.CallbackQuery):
    parts = c.data.split("_", 2)
    if len(parts) < 3:
        await c.answer()
        return
    phone = get_lead_phone_by_prefix(parts[1]) or parts[1]
    source = parts[2].replace("_", " ").strip()
    execute_query("UPDATE leads SET source = ? WHERE phone = ?", (source, phone))
    await c.message.edit_text(f"✅ Источник установлен: {source}")
    await c.answer()

@dp.callback_query(F.data.startswith("re_"))
async def lead_reassign_start(c: types.CallbackQuery):
    payload = c.data[3:]
    idx = payload.rfind("_")
    if idx > 0 and payload[idx + 1 :].isdigit():
        phone = payload[:idx]
        new_uid = payload[idx + 1 :]
        full_phone = get_lead_phone_by_prefix(phone)
        if not full_phone:
            await c.answer("Лид не найден.")
            return
        phone = full_phone
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
            kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
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
        cb = f"re_{phone[:30]}_{uid}"
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
    phone_raw = (m.text or "").replace("+", "").strip()
    if not phone_raw:
        await m.answer("Введите номер (без +).")
        return
    full_phone = get_lead_phone_by_prefix(phone_raw) or phone_raw
    d = await state.get_data()
    sphere = d.get('new_chat_sphere')
    if sphere is None:
        sphere = get_lead_direction(full_phone)
    await state.update_data(target=full_phone, new_chat_sphere=sphere)
    await state.set_state(Form.waiting_for_reply)
    row = execute_query("SELECT name FROM leads WHERE phone = ?", (full_phone,), fetchone=True)
    name = row[0] if row else full_phone
    if row:
        execute_query("UPDATE leads SET status = 'chatting' WHERE phone = ?", (full_phone,))
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("INSERT OR REPLACE INTO chat_sessions (user_id, phone, last_outgoing_at, reminder_sent) VALUES (?, ?, ?, 0)", (m.from_user.id, full_phone, now_iso))
        await chat_history_delete_messages(m.from_user.id, phone=full_phone)
    msg = await m.answer(
        f"💬 <b>Диалог с {name}</b> ({full_phone})\n\nОтправляйте текст, голос, фото — всё уйдёт в WA. Внизу кнопка «Завершить диалог».",
        reply_markup=get_med_finish_dialog_kb(),
        parse_mode="HTML",
    )
    if row:
        chat_history_add(m.from_user.id, msg.message_id, phone=full_phone, context="session")

@dp.callback_query(F.data.startswith("rp_"))
async def reply_start(c: types.CallbackQuery, state: FSMContext):
    phone_prefix = c.data.split("_", 1)[1].strip()
    if len(phone_prefix) > 30:
        phone_prefix = phone_prefix[:30]
    full_phone = get_lead_phone_by_prefix(phone_prefix) or phone_prefix
    await state.update_data(target=full_phone)
    sphere = get_lead_direction(full_phone)
    await state.update_data(new_chat_sphere=sphere)
    await state.set_state(Form.waiting_for_reply)
    # Переводим лид в режим «прямого коридора»
    execute_query("UPDATE leads SET status = 'chatting' WHERE phone = ?", (full_phone,))
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query("INSERT OR REPLACE INTO chat_sessions (user_id, phone, last_outgoing_at, reminder_sent) VALUES (?, ?, ?, 0)", (c.from_user.id, full_phone, now_iso))
    # Удаляем старые сообщения предыдущей сессии (если были)
    await chat_history_delete_messages(c.from_user.id, phone=full_phone)
    row = execute_query("SELECT name FROM leads WHERE phone = ?", (full_phone,), fetchone=True)
    name = row[0] if row else full_phone
    # Одна инфо-панель + кнопка «Завершить диалог»
    msg = await c.message.answer(
        f"💬 <b>Диалог с {name}</b> ({full_phone})\n\nОтправляйте текст, голос, фото — всё уйдёт в WA. Внизу кнопка «Завершить диалог».",
        reply_markup=get_med_finish_dialog_kb(),
        parse_mode="HTML",
    )
    chat_history_add(c.from_user.id, msg.message_id, phone=full_phone, context="session")
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
    if m.text and "Завершить диалог" in (m.text or ""):
        # Удаляем все сообщения текущей сессии; остаётся только итог и финальная карточка
        await chat_history_delete_messages(m.from_user.id, phone=target)
        execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, target))
        u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
        if not u:
            return
        if sphere == 'med' and u[1] == 'med':
            kb = InlineKeyboardBuilder()
            phone_tel = "+" + "".join(ch for ch in str(target) if ch.isdigit())
            kb.button(text=f"📞 Набрать {phone_tel}", callback_data=f"show_tel_{target[:30]}")
            kb.button(text="❌ Отказ", callback_data=f"med_r_{target[:30]}")
            kb.button(text="⏳ Подумает", callback_data=f"med_t_{target[:30]}")
            kb.button(text="💰 Оплатил", callback_data=f"med_p_{target[:30]}")
            kb.button(text="🚫 НЕ ОТВЕЧАЮТ", callback_data=f"med_n_{target[:30]}")
            kb.adjust(1)
            await m.answer("Итог диалога (нажмите «Набрать» — откроется набор в телефоне):", reply_markup=kb.as_markup())
        else:
            kb = InlineKeyboardBuilder()
            phone_tel = "+" + "".join(ch for ch in str(target) if ch.isdigit())
            kb.button(text=f"📞 Набрать {phone_tel}", callback_data=f"show_tel_{target[:30]}")
            kb.button(text="💰 ОПЛАТИЛ", callback_data=f"f_s_{target[:30]}")
            kb.button(text="⏳ ДУМАЕТ", callback_data=f"f_t_{target[:30]}")
            kb.button(text="❌ ОТКАЗ", callback_data=f"f_r_{target[:30]}")
            kb.button(text="🚫 НЕ ОТВЕТИЛ", callback_data=f"f_n_{target[:30]}")
            kb.adjust(1)
            await m.answer("Итог диалога (нажмите «Набрать» — откроется набор в телефоне):", reply_markup=kb.as_markup())
        await state.clear()
        await m.answer("Выберите итог выше. Меню:", reply_markup=get_main_menu(m.from_user.id))
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
        phone_tel = "+" + "".join(ch for ch in str(target) if ch.isdigit())
        kb.button(text=f"📞 Набрать {phone_tel}", callback_data=f"show_tel_{target[:30]}")
        kb.button(text="❌ Отказ", callback_data=f"med_r_{target[:30]}")
        kb.button(text="⏳ Подумает", callback_data=f"med_t_{target[:30]}")
        kb.button(text="💰 Оплатил", callback_data=f"med_p_{target[:30]}")
        kb.button(text="🚫 НЕ ОТВЕЧАЮТ", callback_data=f"med_n_{target[:30]}")
        kb.adjust(1)
        await m.answer("Итог диалога (нажмите «Набрать» — откроется набор в телефоне):", reply_markup=kb.as_markup())
    else:
        kb = InlineKeyboardBuilder()
        phone_tel = "+" + "".join(ch for ch in str(target) if ch.isdigit())
        kb.button(text=f"📞 Набрать {phone_tel}", callback_data=f"show_tel_{target[:30]}")
        kb.button(text="💰 ОПЛАТИЛ", callback_data=f"f_s_{target[:30]}")
        kb.button(text="⏳ ДУМАЕТ", callback_data=f"f_t_{target[:30]}")
        kb.button(text="❌ ОТКАЗ", callback_data=f"f_r_{target[:30]}")
        kb.button(text="🚫 НЕ ОТВЕТИЛ", callback_data=f"f_n_{target[:30]}")
        kb.adjust(1)
        await m.answer("Итог диалога (нажмите «Набрать» — откроется набор в телефоне):", reply_markup=kb.as_markup())
    await state.clear()
    await m.answer("Выберите итог выше. Меню:", reply_markup=get_main_menu(m.from_user.id))

MED_PACKAGES = ["Пакет 1", "Пакет 2", "Пакет 3", "Первичка", "Вторичка"]

@dp.callback_query(F.data.startswith("med_r_"))
async def med_end_refuse(c: types.CallbackQuery, state: FSMContext):
    """Медицина: Отказ — сначала спрашиваем комментарий."""
    phone = c.data[6:]
    if len(phone) > 30:
        phone = phone[:30]
    full_phone = get_lead_phone_by_prefix(phone) or phone
    await state.update_data(refuse_phone=full_phone)
    await state.set_state(Form.refuse_comment_med)
    await c.message.edit_text("📝 Введите комментарий: почему отказали?")
    await c.answer()


@dp.message(Form.refuse_comment_med)
async def med_refuse_comment_done(m: types.Message, state: FSMContext):
    d = await state.get_data()
    phone = d.get("refuse_phone")
    if not phone:
        await state.clear()
        return
    comment = (m.text or "").strip() or "Без комментария"
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query("UPDATE leads SET status='closed', is_answered=0, comment=?, closed_at=? WHERE phone=? AND direction='med'", (f"Отказ: {comment}", now_iso, phone))
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (phone,))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, phone))
    log_lead_event(phone, "status_change", f"closed: Отказ — {comment[:100]}", m.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (m.from_user.id,))
    text, kbd = build_lead_card(phone)
    await m.answer(text or "✅ Отказ зафиксирован.", reply_markup=kbd, parse_mode="HTML")
    await m.answer("Меню", reply_markup=get_main_menu(m.from_user.id))
    await state.clear()
    await try_assign_queued_lead_to_manager(m.from_user.id, 'med')

@dp.callback_query(F.data.startswith("med_t_"))
async def med_end_think(c: types.CallbackQuery, state: FSMContext):
    phone = c.data[6:]
    if len(phone) > 30:
        phone = phone[:30]
    full_phone = get_lead_phone_by_prefix(phone) or phone
    execute_query("UPDATE leads SET status='thinking', is_answered=1, last_touch=? WHERE phone=? AND (direction='med' OR direction IS NULL)", (datetime.now().strftime("%Y-%m-%d %H:%M"), full_phone))
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (full_phone,))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (c.from_user.id, full_phone))
    log_lead_event(full_phone, "status_change", "thinking: Подумает", c.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
    try:
        await c.message.delete()
    except Exception:
        pass
    text, kbd = build_lead_card(full_phone)
    await c.message.answer(text or "⏳ Подумает.", reply_markup=kbd, parse_mode="HTML")
    await c.message.answer("Меню", reply_markup=get_main_menu(c.from_user.id))
    await state.clear()
    await try_assign_queued_lead_to_manager(c.from_user.id, 'med')
    await c.answer()

@dp.callback_query(F.data.startswith("med_n_"))
async def med_end_no_answer(c: types.CallbackQuery):
    """Медицина: НЕ ОТВЕЧАЮТ — лид в дожим (без логики 7 касаний)."""
    phone = c.data[6:]
    if len(phone) > 30:
        await c.answer()
        return
    full_phone = get_lead_phone_by_prefix(phone) or phone
    row = execute_query("SELECT name FROM leads WHERE phone = ? AND direction = 'med'", (full_phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    name = row[0] or full_phone
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (c.from_user.id, full_phone))
    execute_query("UPDATE leads SET status='thinking', last_touch=? WHERE phone=? AND direction='med'", (datetime.now(), full_phone))
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (c.from_user.id,))
    await try_assign_queued_lead_to_manager(c.from_user.id, 'med')
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.message.answer(f"🔄 Пациент {name} в дожиме. Откройте «⏳ Дожим» для списка.", reply_markup=get_main_menu(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data.startswith("med_p_"))
async def med_end_paid(c: types.CallbackQuery, state: FSMContext):
    phone = c.data[6:]
    if len(phone) > 30:
        phone = phone[:30]
    full_phone = get_lead_phone_by_prefix(phone) or phone
    # В callback_data передаём короткий префикс (лимит Telegram)
    p_short = full_phone[:20] if len(full_phone) > 20 else full_phone
    kb = InlineKeyboardBuilder()
    for i, pkg in enumerate(MED_PACKAGES):
        kb.button(text=pkg, callback_data=f"medpkg_{p_short}_{i}")
    kb.adjust(2)
    await c.message.edit_text("Выберите услугу/пакет:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("medpkg_"))
async def med_paid_package(c: types.CallbackQuery, state: FSMContext):
    parts = c.data.split("_", 2)
    if len(parts) < 3:
        await c.answer()
        return
    phone_prefix = parts[1]
    full_phone = get_lead_phone_by_prefix(phone_prefix) or phone_prefix
    idx = int(parts[2]) if parts[2].isdigit() else 0
    pkg = MED_PACKAGES[idx] if 0 <= idx < len(MED_PACKAGES) else MED_PACKAGES[0]
    await state.update_data(med_paid_phone=full_phone, med_paid_service=pkg)
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
        "UPDATE leads SET status='closed', is_answered=1, service=?, payment=COALESCE(payment,0)+?, payment_date=date('now'), massage_sessions=?, closed_at=? WHERE phone=? AND direction='med'",
        (service, s, new_sessions, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), phone),
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
    await state.update_data(c_sphere=m.text)
    await state.set_state(Form.closing_pain)
    await m.answer("Боль клиента:")

@dp.message(Form.closing_pain)
async def cl_pain(m: types.Message, state: FSMContext):
    await state.update_data(c_pain=m.text)
    await state.set_state(Form.closing_comment)
    await m.answer("Комментарий:")

@dp.message(Form.closing_comment)
async def cl_comment(m: types.Message, state: FSMContext):
    await state.update_data(c_comment=m.text)
    await state.set_state(Form.closing_callback)
    await m.answer("Перезвон (дата/время или «нет»):")

@dp.message(Form.closing_callback)
async def cl_fin(m: types.Message, state: FSMContext):
    d = await state.get_data()
    perezvon = (m.text or "").strip()
    st = "thinking" if d['c_status'] == 't' else "closed"
    ans = 1 if d['c_status'] in ['s', 't', 'r'] else 0
    c_phone = d['c_phone']
    c_sphere = d.get('c_sphere') or ''
    c_pain = d.get('c_pain') or ''
    c_comment = d.get('c_comment') or ''
    if st == "closed":
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        is_sale = 1 if d['c_status'] == 's' else 0  # только «Оплатил» = продажа для плана/KPI
        execute_query("UPDATE leads SET status=?, sphere=?, comment=?, is_answered=?, is_sale=?, closed_at=? WHERE phone=?", (st, c_sphere, c_comment, ans, is_sale, now_iso, c_phone))
        # Запись в Google Sheet «База данных лидов» (Дата, ФИО, Номер, Вид бизнеса, Боль клиента, Комментарий, Перезвон)
        lead_row = execute_query("SELECT name FROM leads WHERE phone = ?", (c_phone,), fetchone=True)
        lead_name = (lead_row[0] if lead_row else None) or c_phone
        date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        try:
            await asyncio.to_thread(_append_biz_lead_to_sheet, date_str, lead_name, c_phone, c_sphere, c_pain, c_comment, perezvon)
        except Exception as e:
            logging.warning("CRM: sheet append thread error: %s", e)
        execute_query("UPDATE leads SET status=?, sphere=?, comment=?, is_answered=? WHERE phone=?", (st, c_sphere, c_comment, ans, c_phone))
    execute_query("DELETE FROM follow_up_queue WHERE phone = ?", (c_phone,))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (m.from_user.id, c_phone))
    log_lead_event(c_phone, "status_change", f"{st}: {c_comment[:100]}", m.from_user.id)
    execute_query("UPDATE users SET is_busy=0 WHERE user_id=?", (m.from_user.id,))
    outcome_mid = d.get("outcome_message_id")
    if outcome_mid:
        try:
            await bot.delete_message(chat_id=m.from_user.id, message_id=outcome_mid)
        except Exception:
            pass
    text, kbd = build_lead_card(c_phone)
    await m.answer(text or "✅ Отчет принят!", reply_markup=kbd, parse_mode="HTML")
    await m.answer("Меню", reply_markup=get_main_menu(m.from_user.id))
    await state.clear()
    u = execute_query("SELECT sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    mgr_sphere = (u[0] or 'biz') if u else 'biz'
    await try_assign_queued_lead_to_manager(m.from_user.id, mgr_sphere)

@dp.message(F.text == "🎯 Поставить План")
async def p_list(m: types.Message, state: FSMContext):
    data = await state.get_data()
    sphere = data.get("owner_current_sphere")
    if not is_owner(m.from_user.id):
        sphere = None
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

@dp.message(F.text == "🎯 Назначить План")
async def admin_plan_list(m: types.Message, state: FSMContext):
    """Админ бизнеса: выбор менеджера и установка плана продаж."""
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'admin' or (u[1] != 'biz' and u[1] is not None):
        return
    st = execute_query("SELECT user_id, fio, sphere FROM users WHERE role='manager' AND (sphere='biz' OR sphere IS NULL)", fetchall=True)
    if not st:
        await m.answer("Нет менеджеров бизнеса.")
        return
    kb = InlineKeyboardBuilder()
    for row in st:
        sid, fio = row[0], row[1]
        kb.button(text=f"🎯 {fio}", callback_data=f"sp_{sid}")
    await m.answer("Кому назначить план продаж?", reply_markup=kb.adjust(1).as_markup())

@dp.message(F.text == "📥 ВКЛ/ВЫКЛ лидов")
async def admin_leads_toggle_list(m: types.Message):
    """Админ бизнеса: список менеджеров с переключателем приёма лидов."""
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'admin' or (u[1] != 'biz' and u[1] is not None):
        return
    rows = execute_query(
        "SELECT user_id, fio, COALESCE(can_receive_leads, 1) FROM users WHERE role = 'manager' AND (sphere = 'biz' OR sphere IS NULL OR TRIM(COALESCE(sphere,'')) = '') ORDER BY fio",
        fetchall=True,
    )
    if not rows:
        await m.answer("Нет менеджеров бизнеса.")
        return
    kb = InlineKeyboardBuilder()
    for uid, fio, on in rows:
        label = fio or str(uid)
        status = "🟢 ВКЛ" if on else "🔴 ВЫКЛ"
        kb.button(text=f"{status} {label}", callback_data=f"lead_tgl_{uid}")
    kb.adjust(1)
    await m.answer("Нажмите на менеджера, чтобы включить или выключить приём лидов ему:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("lead_tgl_"))
async def admin_leads_toggle_cb(c: types.CallbackQuery):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not u or u[0] != 'admin' or (u[1] != 'biz' and u[1] is not None):
        await c.answer()
        return
    uid = c.data.replace("lead_tgl_", "").strip()
    if not uid.isdigit():
        await c.answer()
        return
    uid = int(uid)
    row = execute_query("SELECT fio, COALESCE(can_receive_leads, 1) FROM users WHERE user_id = ? AND role = 'manager'", (uid,), fetchone=True)
    if not row:
        await c.answer("Менеджер не найден.")
        return
    fio, current = row[0], row[1]
    new_val = 0 if current else 1
    execute_query("UPDATE users SET can_receive_leads = ? WHERE user_id = ?", (new_val, uid))
    status = "🟢 ВКЛ" if new_val else "🔴 ВЫКЛ"
    await c.answer(f"{fio or uid}: лиды {status}")
    # Обновить список
    rows = execute_query(
        "SELECT user_id, fio, COALESCE(can_receive_leads, 1) FROM users WHERE role = 'manager' AND (sphere = 'biz' OR sphere IS NULL OR TRIM(COALESCE(sphere,'')) = '') ORDER BY fio",
        fetchall=True,
    )
    kb = InlineKeyboardBuilder()
    for ruid, rfio, on in rows:
        label = rfio or str(ruid)
        st = "🟢 ВКЛ" if on else "🔴 ВЫКЛ"
        kb.button(text=f"{st} {label}", callback_data=f"lead_tgl_{ruid}")
    kb.adjust(1)
    try:
        await c.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception:
        pass

@dp.callback_query(F.data.startswith("sp_"))
async def p_val(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(tm=c.data.split("_")[1]); await state.set_state(Form.setting_plan_value); await c.message.answer("Введите план:"); await c.answer()

@dp.message(Form.setting_plan_value)
async def p_save(m: types.Message, state: FSMContext):
    d = await state.get_data(); execute_query("UPDATE users SET plan = ? WHERE user_id = ?", (m.text, d['tm']))
    await m.answer(f"✅ План {m.text} сохранен!"); await state.clear()

@dp.message(F.text == "👥 Мои клиенты")
async def biz_my_clients(m: types.Message):
    """Менеджер бизнеса: свои лиды, которые ещё не закрыты (новые и в диалоге). Аналог «Мои Пациенты» в медицине."""
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'manager' or (u[1] != 'biz' and u[1] is not None):
        return
    leads = execute_query(
        "SELECT phone, name, status FROM leads WHERE manager_id = ? AND " + BIZ_LEAD_COND + " AND status IN ('active', 'chatting') ORDER BY last_touch DESC",
        (m.from_user.id,),
        fetchall=True,
    )
    if not leads:
        await m.answer("Нет клиентов, которым вы ещё не ответили. Закройте старых в «⏳ Дожим» / «✅ Оплачено» / «❌ Отказ» — тогда будут приходить новые лиды.")
        return
    status_label = {"active": "🟢 новый", "chatting": "💬 в диалоге"}
    lines = ["👥 <b>Мои клиенты</b> — кому вы ещё не ответили:\n"]
    kb = InlineKeyboardBuilder()
    for phone, name, st in leads:
        lbl = status_label.get(st, st)
        lines.append(f"• {name} ({phone}) — {lbl}")
        kb.button(text=f"✍️ {name}", callback_data=f"rp_{phone[:30]}")
        kb.button(text=f"📞 {name}", callback_data=f"show_tel_{phone[:30]}")
        kb.button(text="📋 Итог звонка", callback_data=f"cl_{phone[:30]}")
    kb.adjust(3)
    await m.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@dp.message(F.text.in_(["⏳ Дожим", "✅ Оплачено", "❌ Отказ"]))
async def mgr_lists(m: types.Message):
    st_map = {"⏳ Дожим": "thinking", "✅ Оплачено": "closed", "❌ Отказ": "closed"}
    leads = execute_query("SELECT phone, name FROM leads WHERE manager_id = ? AND status = ? AND " + BIZ_LEAD_COND, (m.from_user.id, st_map[m.text]), fetchall=True)
    if not leads:
        if m.text == "⏳ Дожим":
            return await m.answer("⏳ Дожим — клиенты в ожидании (не ответили / думает).\n\nСписок пуст.")
        return await m.answer("Список пуст.")
    if m.text == "⏳ Дожим":
        lines = ["⏳ <b>Дожим</b> — клиенты в ожидании (не ответили / думает):\n"]
        for p, n in leads:
            lines.append(f"• {n} ({p})")
        kb = InlineKeyboardBuilder()
        for p, n in leads:
            kb.button(text=f"✍️ {n}", callback_data=f"rp_{p[:30]}")
            kb.button(text=f"📞 {n}", callback_data=f"show_tel_{p[:30]}")
            kb.button(text="📋 Итог", callback_data=f"cl_{p[:30]}")
        kb.adjust(3)
        await m.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        kb = InlineKeyboardBuilder()
        for p, n in leads:
            kb.button(text=f"👤 {n}", callback_data=f"rp_{p[:30]}")
        await m.answer(f"Ваши клиенты ({m.text}):", reply_markup=kb.adjust(1).as_markup())

@dp.message(F.text.in_(["🟢 ВКЛ ЛИДЫ", "🔴 ВЫКЛ ЛИДЫ"]))
async def tgl(m: types.Message):
    """Глобальное вкл/выкл раздачи лидов по направлению бизнес. Только владелец."""
    if not is_owner(m.from_user.id):
        return
    c = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
    v = '0' if c and c[0] == '1' else '1'; execute_query("INSERT OR REPLACE INTO settings (key, value) VALUES ('leads_enabled', ?)", (v,))
    await m.answer(f"Статус: {'ВКЛ' if v=='1' else 'ВЫКЛ'}", reply_markup=get_main_menu(m.from_user.id))

@dp.message(F.text == "📋 Распределить лиды")
async def distribute_leads_now(m: types.Message):
    """Владелец: раздать все pending-лиды бизнеса свободным менеджерам."""
    if not is_owner(m.from_user.id):
        return
    cond = BIZ_LEAD_COND
    # Если приём лидов выключен — свободных для раздачи нет
    l_on = execute_query("SELECT value FROM settings WHERE key = 'leads_enabled'", fetchone=True)
    if l_on is not None and str(l_on[0]).strip() == '0':
        pending = execute_query(f"SELECT COUNT(*) FROM leads WHERE status = 'pending' AND {cond}{NOT_TEST_LEAD_COND}", (), fetchone=True)[0] or 0
        await m.answer(
            f"🔴 Приём лидов выключен (ВЫКЛ ЛИДЫ). Включите кнопку «🟢 ВКЛ ЛИДЫ» — тогда «Распределить лиды» сможет раздать {pending} из очереди.",
            reply_markup=get_main_menu(m.from_user.id),
        )
        return
    n = await distribute_pending_biz_leads()
    pending_after = execute_query(f"SELECT COUNT(*) FROM leads WHERE status = 'pending' AND {cond}{NOT_TEST_LEAD_COND}", (), fetchone=True)[0] or 0
    total_biz_mgrs = execute_query(
        "SELECT COUNT(*) FROM users u WHERE (LOWER(u.role)='manager' AND (u.sphere='biz' OR u.sphere IS NULL OR TRIM(COALESCE(u.sphere,''))='')) OR (LOWER(u.role)='admin' AND (u.sphere='biz' OR u.sphere IS NULL OR TRIM(COALESCE(u.sphere,''))='') AND COALESCE(u.can_receive_leads,0)=1)",
        (),
        fetchone=True,
    )[0] or 0
    msg = f"✅ Распределено лидов: {n}. В очереди осталось: {pending_after}."
    if n == 0 and pending_after > 0:
        in_progress = execute_query(
            f"SELECT COUNT(*) FROM leads WHERE status IN ('active', 'chatting') AND manager_id IS NOT NULL AND {cond}{NOT_TEST_LEAD_COND}",
            (),
            fetchone=True,
        )[0] or 0
        msg += f"\n\nСвободных менеджеров бизнеса нет (все {total_biz_mgrs} заняты). Когда менеджер закроет лид — ему автоматически выдастся следующий из очереди."
        if in_progress > 0:
            msg += f"\n\nДиагностика: лидов «в работе» (бизнес): {in_progress} — они занимают менеджеров. Нажми «📥 Вернуть лиды в очередь», затем «📋 Распределить лиды»."
        else:
            msg += "\n\nПроверьте: включён ли приём лидов (🟢 ВКЛ ЛИДЫ) и есть ли менеджеры со сферой «Бизнес»."
    await m.answer(msg, reply_markup=get_main_menu(m.from_user.id))

@dp.message(F.text == "📋 Лиды в работе")
async def wl(m: types.Message, state: FSMContext):
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u:
        return
    role, user_sphere = u[0], u[1]
    data = await state.get_data()
    owner_sphere = data.get("owner_current_sphere")
    if (is_owner(m.from_user.id) and owner_sphere == "med") or (role == 'admin' and user_sphere == 'med'):
        cond = "status IN ('active', 'chatting') AND direction = 'med' AND manager_id IS NOT NULL"
        title = "📋 <b>В РАБОТЕ (Медицина):</b>"
    else:
        cond = "status IN ('active', 'chatting') AND " + BIZ_LEAD_COND + " AND manager_id IS NOT NULL"
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
        kb.button(text=f"↩ Перенаправить: {name}", callback_data=f"reas_{phone[:30]}")
    await m.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.adjust(1).as_markup())


@dp.callback_query(F.data.startswith("reas_"))
async def reassign_lead_choose(c: types.CallbackQuery, state: FSMContext):
    """Выбор менеджера для перенаправления лида. Может владелец или админ своего направления."""
    cur = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
    if not cur:
        await c.answer()
        return
    role, user_sphere = cur[0], cur[1]
    phone_prefix = c.data.replace("reas_", "").strip()
    phone = get_lead_phone_by_prefix(phone_prefix)
    if not phone:
        phone = phone_prefix
    row = execute_query("SELECT name, manager_id, direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    name, old_mgr_id, direction = row[0], row[1], (row[2] or "biz")
    if not is_owner(c.from_user.id) and not (role == 'admin' and (user_sphere or 'biz') == direction):
        await c.answer()
        return
    await state.update_data(reassign_phone=phone, reassign_old_mgr=old_mgr_id, reassign_direction=direction)
    managers = get_managers_by_direction(direction)
    kb = InlineKeyboardBuilder()
    for uid, fio in managers:
        if uid != old_mgr_id:
            kb.button(text=f"→ {fio}", callback_data=f"reto_{phone[:30]}_{uid}")
    if not kb.buttons:
        await c.answer("Нет других менеджеров в этом направлении.")
        return
    await c.message.answer(f"Кому перенаправить лида <b>{name}</b> ({phone})?", reply_markup=kb.adjust(1).as_markup(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("reto_"))
async def reassign_lead_do(c: types.CallbackQuery, state: FSMContext):
    """Перенаправить лид другому менеджеру и удалить из чата старого. Может владелец или админ своего направления."""
    if not is_owner(c.from_user.id):
        cur = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
        if not cur or cur[0] != 'admin':
            await c.answer()
            return
        # Проверка направления лида будет ниже после получения row
    parts = c.data.split("_")
    if len(parts) < 3:
        await c.answer()
        return
    phone = parts[1][:30]
    full_phone = get_lead_phone_by_prefix(phone)
    if not full_phone:
        await c.answer("Лид не найден.")
        return
    phone = full_phone
    new_mgr_id = int(parts[2])
    row = execute_query("SELECT name, manager_id, direction FROM leads WHERE phone = ?", (phone,), fetchone=True)
    if not row:
        await c.answer("Лид не найден.")
        return
    name, old_mgr_id, lead_dir = row[0], row[1], (row[2] or "biz")
    if not is_owner(c.from_user.id):
        cur = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (c.from_user.id,), fetchone=True)
        if not cur or cur[0] != 'admin' or (cur[1] or 'biz') != lead_dir:
            await c.answer("Нет прав на перенаправление этого лида.")
            return
    execute_query("UPDATE leads SET manager_id = ? WHERE phone = ?", (new_mgr_id, phone))
    execute_query("DELETE FROM chat_sessions WHERE user_id = ? AND phone = ?", (old_mgr_id, phone))
    if old_mgr_id:
        await chat_history_delete_messages(old_mgr_id, phone=phone)
        try:
            await bot.send_message(old_mgr_id, f"📤 Лид <b>{name}</b> ({phone}) перенаправлен другому менеджеру.", parse_mode="HTML")
        except Exception:
            pass
    kb = InlineKeyboardBuilder().button(text="📞 ПОЗВОНИТЬ", callback_data=f"cl_{phone[:30]}").button(text="✍️ НАПИСАТЬ", callback_data=f"rp_{phone[:30]}").adjust(2).as_markup()
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
    if not u or (u[0] != 'owner' and not (u[0] == 'admin' and u[1] == 'med')):
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
    period_cond = " (last_touch >= ? OR payment_date >= ?) "
    all_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND {period_cond}", (start_str, start_str[:10]), fetchone=True)[0] or 0
    ans_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND is_answered=1 AND {period_cond}", (start_str, start_str[:10]), fetchone=True)[0]
    sold_l = execute_query(f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND status='closed' AND is_answered=1 AND (comment IS NULL OR comment NOT LIKE '%Автозакрытие%') AND {period_cond}", (start_str, start_str[:10]), fetchone=True)[0]
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
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] != 'owner' and not (u[0] == 'admin' and u[1] == 'biz')):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Выгрузить всё (бизнес)", callback_data="export_biz_all")
    kb.button(text="За период (даты)", callback_data="export_biz_period")
    await m.answer("Выгрузка лидов бизнеса:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data == "export_biz_all")
async def export_biz_all_cb(c: types.CallbackQuery):
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "crm.xlsx")
    with sqlite3.connect(DB_PATH) as conn:
        pd.read_sql_query("SELECT * FROM leads WHERE " + BIZ_LEAD_COND, conn).to_excel(path, index=False)
    await c.message.answer_document(types.FSInputFile(path))
    await c.answer()

@dp.callback_query(F.data == "export_biz_period")
async def export_biz_period_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(Form.export_biz_period)
    await c.message.answer("Введите период для выгрузки (одна дата или две через пробел):\n2026-02-24\nили 2026-02-01 2026-02-24")
    await c.answer()

@dp.message(Form.export_biz_period)
async def export_biz_period_done(m: types.Message, state: FSMContext):
    import tempfile
    parts = (m.text or "").strip().split()
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
        await m.answer("Неверный формат. Пример: 2026-02-24 или 2026-02-01 2026-02-24")
        return
    if d2 < d1:
        d1, d2 = d2, d1
    start_str = datetime.combine(d1, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.combine(d2, datetime.max.time()).strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(tempfile.gettempdir(), "crm_export.xlsx")
    with sqlite3.connect(DB_PATH) as conn:
        pd.read_sql_query(
            "SELECT * FROM leads WHERE " + BIZ_LEAD_COND + " AND created_at BETWEEN ? AND ?",
            (start_str, end_str), conn
        ).to_excel(path, index=False)
    await m.answer_document(types.FSInputFile(path))
    await m.answer(f"✅ Выгружено за период {d1} — {d2}.")
    await state.clear()

@dp.message(F.text == "⏱ Время в работе")
async def time_in_work_report(m: types.Message):
    u = execute_query("SELECT role FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'owner':
        return
    lines = ["⏱ <b>Среднее время в работе</b> (от создания лида до закрытия):\n"]
    for direction, cond in [("Бизнес", BIZ_LEAD_COND + " AND is_sale = 1"), ("Медицина", "direction = 'med'")]:
        row = execute_query(
            f"SELECT AVG((julianday(closed_at) - julianday(created_at)) * 24) FROM leads WHERE status = 'closed' AND closed_at IS NOT NULL AND created_at IS NOT NULL AND {cond}{NOT_TEST_LEAD_COND}",
            (), fetchone=True)
        avg_h = (row[0] if row and row[0] is not None else None) or 0
        if avg_h < 24:
            lines.append(f"▪️ {direction}: {round(avg_h, 1)} ч")
        else:
            lines.append(f"▪️ {direction}: {round(avg_h / 24, 1)} дн")
    cnt_biz = execute_query("SELECT COUNT(*) FROM leads WHERE status = 'closed' AND closed_at IS NOT NULL AND created_at IS NOT NULL AND is_sale = 1 AND " + BIZ_LEAD_COND + NOT_TEST_LEAD_COND, (), fetchone=True)[0] or 0
    cnt_med = execute_query("SELECT COUNT(*) FROM leads WHERE status = 'closed' AND closed_at IS NOT NULL AND created_at IS NOT NULL AND direction = 'med'" + NOT_TEST_LEAD_COND, (), fetchone=True)[0] or 0
    lines.append(f"\n(по {cnt_biz} и {cnt_med} закрытым лидам)")
    await m.answer("\n".join(lines), parse_mode="HTML")

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
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or (u[0] != 'owner' and not (u[0] == 'admin' and u[1] == 'med')):
        return
    st = execute_query("SELECT user_id, fio, plan FROM users WHERE role='manager' AND sphere='med'", fetchall=True)
    txt = "🎯 <b>План/KPI (Медицина)</b>\n\nПоказатели: План, Дозвон, Касания.\n\n"
    for mid, fio, plan in st:
        m_all = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND direction='med'" + NOT_TEST_LEAD_COND, (mid,), fetchone=True)[0] or 1
        m_ans = execute_query("SELECT COUNT(*) FROM leads WHERE manager_id=? AND direction='med' AND is_answered=1" + NOT_TEST_LEAD_COND, (mid,), fetchone=True)[0]
        m_t = execute_query("SELECT AVG(touches) FROM leads WHERE manager_id=? AND direction='med'" + NOT_TEST_LEAD_COND, (mid,), fetchone=True)[0] or 0
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
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND is_answered = 1 AND created_at BETWEEN ? AND ?",
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
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND created_at BETWEEN ? AND ?",
        (start_str, end_str),
        fetchone=True,
    )[0] or 0
    processed = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {cond}{NOT_TEST_LEAD_COND} AND is_answered = 1 AND created_at BETWEEN ? AND ?",
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

@dp.message(F.text == "👥 Мои Пациенты")
async def med_my_patients(m: types.Message):
    """Менеджер медицины: пациенты, привязанные к нему, которым он ещё не ответил (новые и в диалоге). Дожим — отдельно в «⏳ Дожим»."""
    u = execute_query("SELECT role, sphere FROM users WHERE user_id = ?", (m.from_user.id,), fetchone=True)
    if not u or u[0] != 'manager' or u[1] != 'med':
        return
    leads = execute_query(
        "SELECT phone, name, status FROM leads WHERE manager_id = ? AND direction = 'med' AND status IN ('active', 'chatting') ORDER BY last_touch DESC",
        (m.from_user.id,),
        fetchall=True,
    )
    if not leads:
        await m.answer("Нет пациентов, которым вы ещё не ответили. Новые лиды появятся здесь; тех, кто в ожидании — смотрите в «⏳ Дожим».")
        return
    status_label = {"active": "🟢 новый", "chatting": "💬 в диалоге"}
    lines = ["👥 <b>Мои Пациенты</b> — кому вы ещё не ответили:\n"]
    kb = InlineKeyboardBuilder()
    for phone, name, st in leads:
        lbl = status_label.get(st, st)
        lines.append(f"• {name} ({phone}) — {lbl}")
        kb.button(text=f"✍️ {name}", callback_data=f"rp_{phone[:30]}")
        kb.button(text=f"📞 {name}", callback_data=f"show_tel_{phone[:30]}")
        kb.button(text="📋 Итог звонка", callback_data=f"cl_{phone[:30]}")
    kb.adjust(3)
    await m.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


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
            kb.button(text=f"✍️ {name}", callback_data=f"rp_{phone[:30]}")
            kb.button(text=f"📞 {name}", callback_data=f"show_tel_{phone[:30]}")
            kb.button(text="📋 Итог", callback_data=f"cl_{phone[:30]}")
        kb.adjust(3)
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
        await m.answer("⏳ Дожим — пациенты в ожидании (не ответили / подумает).\n\nСписок пуст.")
        return
    lines = ["⏳ <b>Дожим</b> — пациенты в ожидании (не ответили / подумает):\n"]
    kb = InlineKeyboardBuilder()
    for phone, name in leads:
        lines.append(f"• {name} ({phone})")
        kb.button(text=f"✍️ {name}", callback_data=f"rp_{phone[:30]}")
        kb.button(text=f"📞 {name}", callback_data=f"show_tel_{phone[:30]}")
        kb.button(text="📋 Итог", callback_data=f"cl_{phone[:30]}")
    kb.adjust(3)
    await m.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())

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
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("INSERT INTO leads (phone, name, status, manager_id, direction, massage_sessions, created_at, last_touch, source) VALUES (?, 'Пациент', 'active', ?, 'med', 1, ?, ?, 'WhatsApp')", (phone, get_first_owner_id(), now_iso, now_iso))
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

@dp.message(Command("fix_biz_busy"))
async def fix_biz_busy(m: types.Message):
    """Владелец: сбросить is_busy у тех менеджеров бизнеса, у кого нет лидов в работе (active/chatting). Исправляет «застрявший» флаг."""
    if not is_owner(m.from_user.id):
        return
    cond = BIZ_LEAD_COND
    execute_query(
        """UPDATE users SET is_busy = 0 WHERE (LOWER(role)='manager' AND (sphere='biz' OR sphere IS NULL OR TRIM(COALESCE(sphere,''))='')) OR (LOWER(role)='admin' AND (sphere='biz' OR sphere IS NULL OR TRIM(COALESCE(sphere,''))='') AND COALESCE(can_receive_leads,0)=1)
           AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.manager_id = users.user_id AND l.status IN ('active', 'chatting') AND (l.direction = 'biz' OR l.direction IS NULL))"""
    )
    await m.answer("✅ Сброшен is_busy у менеджеров бизнеса без активных лидов. Нажми «📋 Распределить лиды» — лиды из очереди раздадутся.")

@dp.message(Command("debug_biz"))
async def debug_biz(m: types.Message):
    """Владелец: кто из менеджеров бизнеса и сколько у них лидов в работе (active/chatting)."""
    if not is_owner(m.from_user.id):
        return
    mgrs = execute_query(
        "SELECT u.user_id, u.fio, u.sphere FROM users u WHERE (LOWER(u.role)='manager' AND (u.sphere='biz' OR u.sphere IS NULL OR TRIM(COALESCE(u.sphere,''))='')) OR (LOWER(u.role)='admin' AND (u.sphere='biz' OR u.sphere IS NULL OR TRIM(COALESCE(u.sphere,''))='') AND COALESCE(u.can_receive_leads,0)=1)",
        (),
        fetchall=True,
    )
    lead_cond = BIZ_LEAD_COND
    lines = ["🔍 <b>DEBUG: Бизнес</b>\n"]
    if not mgrs:
        lines.append("❌ Менеджеров бизнеса в базе нет.")
        await m.answer("\n".join(lines), parse_mode="HTML")
        return
    for uid, fio, sph in mgrs:
        cnt = execute_query(
            f"SELECT COUNT(*) FROM leads WHERE manager_id = ? AND status IN ('active', 'chatting') AND {lead_cond}{NOT_TEST_LEAD_COND}",
            (uid,),
            fetchone=True,
        )[0] or 0
        st = "🟢 свободен" if cnt == 0 else f"🔴 в работе: {cnt}"
        lines.append(f"• {fio or uid} (sphere={sph!r}) — {st}")
    pending = execute_query(f"SELECT COUNT(*) FROM leads WHERE status = 'pending' AND {lead_cond}{NOT_TEST_LEAD_COND}", (), fetchone=True)[0] or 0
    lines.append(f"\n📥 В очереди: {pending}")
    free = get_free_manager_for_direction('biz')
    lines.append(f"\nСвободный для раздачи: {'да (id=' + str(free) + ')' if free else 'нет'}")
    await m.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("biz_back_to_queue"))
async def biz_back_to_queue(m: types.Message):
    """Владелец: всех лидов бизнеса со статусом active/chatting перевести в очередь (pending, без менеджера). Менеджеры станут свободны."""
    if not is_owner(m.from_user.id):
        return
    count = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND} AND status IN ('active', 'chatting')",
        (),
        fetchone=True,
    )[0] or 0
    execute_query(
        f"UPDATE leads SET status = 'pending', manager_id = NULL WHERE {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND} AND status IN ('active', 'chatting')"
    )
    execute_query("UPDATE users SET is_busy = 0")
    if count > 0:
        await m.answer(f"✅ В очередь переведено лидов: {count}. Менеджеры свободны. Нажми «📋 Распределить лиды».")
    else:
        await m.answer("✅ Лидов «в работе» не было (0). Если «Распределить лиды» пишет «все заняты» — включите 🟢 ВКЛ ЛИДЫ и проверьте, что есть менеджеры со сферой Бизнес.")

@dp.message(F.text == "📥 Вернуть лиды в очередь")
async def biz_back_to_queue_btn(m: types.Message):
    """Кнопка: то же, что /biz_back_to_queue."""
    if not is_owner(m.from_user.id):
        return
    count = execute_query(
        f"SELECT COUNT(*) FROM leads WHERE {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND} AND status IN ('active', 'chatting')",
        (),
        fetchone=True,
    )[0] or 0
    execute_query(
        f"UPDATE leads SET status = 'pending', manager_id = NULL WHERE {BIZ_LEAD_COND}{NOT_TEST_LEAD_COND} AND status IN ('active', 'chatting')"
    )
    execute_query("UPDATE users SET is_busy = 0")
    if count > 0:
        await m.answer(f"✅ В очередь переведено лидов: {count}. Менеджеры свободны. Нажми «📋 Распределить лиды».")
    else:
        await m.answer("✅ Лидов «в работе» не было (0). Если «Распределить лиды» пишет «все заняты» — включите 🟢 ВКЛ ЛИДЫ и проверьте менеджеров со сферой Бизнес.")

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
    if _tg_leads and TELEGRAM_LEADS_PHONE and TELEGRAM_API_ID and TELEGRAM_API_HASH:
        asyncio.create_task(_tg_leads.run_client(TELEGRAM_LEADS_PHONE, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH, _on_telegram_lead_message))
        logging.info("tg_leads: task started for %s", TELEGRAM_LEADS_PHONE)
    else:
        if not (TELEGRAM_LEADS_PHONE and TELEGRAM_API_ID and TELEGRAM_API_HASH):
            logging.info("tg_leads: не запущен — задайте TELEGRAM_LEADS_PHONE, TELEGRAM_API_ID, TELEGRAM_API_HASH в переменных Amvera")
    asyncio.create_task(job_remind_24h())
    asyncio.create_task(job_chatting_idle())
    asyncio.create_task(job_tasks_reminder())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())