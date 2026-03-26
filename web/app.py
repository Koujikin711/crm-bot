# Веб-интерфейс CRM: вход через Telegram + сайдбар по ролям
# Запуск из папки CRM: python -m web.app   или  flask --app web.app run --host 0.0.0.0 --port 5000
import os
import hmac
import hashlib
from pathlib import Path

# Подключаем корень проекта для config/db
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_project_root))

# Создаём таблицы в БД, если их ещё нет (та же БД, что у бота)
try:
    from db import init_db
    init_db()
except Exception:
    pass

from flask import Flask, redirect, request, session, url_for, render_template

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)
app.secret_key = os.environ.get("WEB_SECRET_KEY", "crm-web-secret-change-in-production")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

BOT_USERNAME = os.environ.get("BOT_USERNAME", "MetodiCRM_bot")


def _get_config():
    import sys
    try:
        from config import API_TOKEN, DB_PATH
        return API_TOKEN, DB_PATH
    except Exception:
        pass
    DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(_project_root, "crm_base.db"))
    API_TOKEN = os.environ.get("API_TOKEN", "")
    return API_TOKEN, DB_PATH


def execute_query(query, params=(), fetchone=False, fetchall=False):
    import sqlite3
    API_TOKEN, DB_PATH = _get_config()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        conn.commit()


def verify_telegram_login(data, token):
    """Проверка hash от Telegram Login Widget."""
    received = data.get("hash")
    if not received:
        return False
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if k != "hash")
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def get_menu_items(role, sphere, can_receive_leads=0):
    """Пункты меню для сайдбара (как в get_main_menu бота)."""
    if role == "owner":
        return [
            {"text": "Медицина", "icon": "🏥", "href": "#med"},
            {"text": "Бизнес", "icon": "💼", "href": "#biz"},
        ]
    if role == "admin" and sphere == "med":
        return [
            {"text": "Нагруженность", "icon": "📊", "href": "#"},
            {"text": "Статистика лидов", "icon": "📈", "href": "#"},
            {"text": "Записать к Ганчине", "icon": "📅", "href": "#"},
            {"text": "Лиды в работе", "icon": "📋", "href": "#"},
            {"text": "Поступления лидов (мед)", "icon": "📥", "href": "#"},
            {"text": "Начать диалог", "icon": "💬", "href": "#"},
            {"text": "Поиск лида", "icon": "🔍", "href": "#"},
            {"text": "Доработать", "icon": "📌", "href": "#"},
            {"text": "Оплаты", "icon": "💰", "href": "#"},
            {"text": "Продлить курс", "icon": "🔄", "href": "#"},
            {"text": "План / KPI", "icon": "🎯", "href": "#"},
        ]
    if role == "manager" and sphere == "med":
        return [
            {"text": "Мои Пациенты", "icon": "👥", "href": "#"},
            {"text": "Мои записи", "icon": "📋", "href": "#"},
            {"text": "Мои оплаты", "icon": "💰", "href": "#"},
            {"text": "Дожим", "icon": "⏳", "href": "#"},
            {"text": "Доработать", "icon": "📌", "href": "#"},
        ]
    if role == "admin" and (sphere == "biz" or sphere is None):
        personal = "Мои лиды: ВКЛ" if can_receive_leads else "Мои лиды: ВЫКЛ"
        return [
            {"text": "Статистика", "icon": "📈", "href": "#"},
            {"text": "KPI Менеджеров", "icon": "👤", "href": "#"},
            {"text": "Лиды в работе", "icon": "📋", "href": "#"},
            {"text": "Начать диалог", "icon": "💬", "href": "#"},
            {"text": "Поиск лида", "icon": "🔍", "href": "#"},
            {"text": "Доработать", "icon": "📌", "href": "#"},
            {"text": "Поступления лидов", "icon": "📥", "href": "#"},
            {"text": personal, "icon": "📥", "href": "#"},
        ]
    if role == "manager" and (sphere == "biz" or sphere is None):
        return [
            {"text": "Мои клиенты", "icon": "👥", "href": "#"},
            {"text": "Дожим", "icon": "⏳", "href": "#"},
            {"text": "Оплачено", "icon": "✅", "href": "#"},
            {"text": "Отказ", "icon": "❌", "href": "#"},
            {"text": "Доработать", "icon": "📌", "href": "#"},
        ]
    return [{"text": "Главная", "icon": "🏠", "href": "#"}]


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login")
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    auth_url = request.host_url.rstrip("/") + url_for("auth_telegram")
    # Для localhost показываем тестовый вход (без Telegram)
    is_local = "localhost" in (request.host or "") or "127.0.0.1" in (request.host or "")
    users = []
    if is_local:
        rows = execute_query(
            "SELECT user_id, COALESCE(fio,''), role, COALESCE(sphere,'') FROM users ORDER BY role, user_id",
            (),
            fetchall=True,
        )
        users = [{"user_id": r[0], "fio": r[1] or f"ID{r[0]}", "role": r[2], "sphere": r[3]} for r in (rows or [])]
    return render_template("login.html", bot_username=BOT_USERNAME, auth_url=auth_url, dev_users=users, is_local=is_local)


@app.route("/auth/dev", methods=["POST"])
def auth_dev():
    """Тестовый вход отключён (оставляем только Telegram login)."""
    return redirect(url_for("login") + "?error=invalid")


@app.route("/auth/telegram", methods=["GET"])
def auth_telegram():
    """Callback от Telegram Login Widget: проверяем hash и создаём сессию."""
    API_TOKEN, _ = _get_config()
    data = {k: v for k, v in request.args.items()}
    if not data or not verify_telegram_login(data, API_TOKEN):
        return redirect(url_for("login") + "?error=invalid")
    user_id = data.get("id")
    if not user_id:
        return redirect(url_for("login") + "?error=no_id")
    try:
        user_id = int(user_id)
    except ValueError:
        return redirect(url_for("login") + "?error=bad_id")
    row = execute_query(
        "SELECT role, sphere, COALESCE(fio,''), COALESCE(can_receive_leads,0) FROM users WHERE user_id = ?",
        (user_id,),
        fetchone=True,
    )
    if not row:
        return redirect(url_for("login") + "?error=not_found")
    role, sphere, fio, can_receive_leads = row[0], row[1], row[2], row[3]
    session["user_id"] = user_id
    session["role"] = role
    session["sphere"] = sphere if sphere else None
    session["fio"] = fio or (data.get("first_name") or "") + " " + (data.get("last_name") or "")
    session["can_receive_leads"] = can_receive_leads
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if not session.get("user_id"):
        return redirect(url_for("login"))
    menu = get_menu_items(
        session.get("role", ""),
        session.get("sphere"),
        session.get("can_receive_leads", 0),
    )
    return render_template(
        "dashboard.html",
        menu=menu,
        fio=session.get("fio", ""),
        role=session.get("role", ""),
        sphere=session.get("sphere") or "—",
    )


if __name__ == "__main__":
    # Порт из PORT или 80 (локальный запуск)
    port = int(os.environ.get("PORT", "80"))
    app.run(host="0.0.0.0", port=port, debug=True)
