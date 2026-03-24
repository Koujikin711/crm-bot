# Веб-интерфейс CRM: вход через Telegram + сайдбар по ролям + MVP онлайн-записи
import os
import hmac
import hashlib
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

# Подключаем корень проекта для config/db
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_project_root))

# Создаем таблицы бота, если их еще нет.
try:
    from db import init_db

    init_db()
except Exception:
    pass

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
    try:
        from config import API_TOKEN, DB_PATH

        return API_TOKEN, DB_PATH
    except Exception:
        pass
    db_path = os.environ.get("CRM_DB_PATH", os.path.join(_project_root, "crm_base.db"))
    api_token = os.environ.get("API_TOKEN", "")
    return api_token, db_path


def execute_query(query, params=(), fetchone=False, fetchall=False):
    _, db_path = _get_config()
    with sqlite3.connect(db_path, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        conn.commit()


def init_booking_tables():
    """MVP таблицы онлайн-записи (без ломки текущей CRM-схемы)."""
    execute_query(
        """CREATE TABLE IF NOT EXISTS medical_directions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            duration_min INTEGER NOT NULL DEFAULT 30,
            is_active INTEGER NOT NULL DEFAULT 1
        )"""
    )
    execute_query(
        """CREATE TABLE IF NOT EXISTS medical_specialists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            direction_id INTEGER NOT NULL,
            phone TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(direction_id) REFERENCES medical_directions(id)
        )"""
    )
    execute_query(
        """CREATE TABLE IF NOT EXISTS medical_appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_phone TEXT,
            patient_name TEXT NOT NULL,
            patient_phone TEXT NOT NULL,
            direction_id INTEGER NOT NULL,
            specialist_id INTEGER NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'booked',
            responsible_manager_id INTEGER,
            created_by_user_id INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(direction_id) REFERENCES medical_directions(id),
            FOREIGN KEY(specialist_id) REFERENCES medical_specialists(id)
        )"""
    )
    execute_query(
        "CREATE INDEX IF NOT EXISTS ix_med_appt_start ON medical_appointments(start_at)"
    )
    execute_query(
        "CREATE INDEX IF NOT EXISTS ix_med_appt_spec ON medical_appointments(specialist_id, start_at)"
    )
    # Seed demo direction/specialist once for quick start.
    execute_query(
        "INSERT OR IGNORE INTO medical_directions (id, name, duration_min, is_active) VALUES (1, 'Консультация', 30, 1)"
    )
    execute_query(
        "INSERT OR IGNORE INTO medical_specialists (id, full_name, direction_id, is_active) VALUES (1, 'Ганчина', 1, 1)"
    )


init_booking_tables()


def verify_telegram_login(data, token):
    """Проверка hash от Telegram Login Widget."""
    received = data.get("hash")
    if not received:
        return False
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if k != "hash")
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def can_access_booking(role, sphere):
    return role == "owner" or (role == "admin" and sphere == "med")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def booking_access_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        role = session.get("role")
        sphere = session.get("sphere")
        if not can_access_booking(role, sphere):
            if request.path.startswith("/web-api/"):
                return jsonify({"detail": "Forbidden"}), 403
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)

    return wrapper


def get_menu_items(role, sphere, can_receive_leads=0):
    """Пункты меню для сайдбара (MVP)."""
    items = [{"text": "Главная", "icon": "🏠", "href": "/dashboard"}]
    if can_access_booking(role, sphere):
        items.append({"text": "Онлайн запись", "icon": "🗓️", "href": "/online-booking"})
        items.append({"text": "Справочники", "icon": "📚", "href": "/online-booking/dictionaries"})
        items.append({"text": "Журнал записей", "icon": "🧾", "href": "/online-booking/journal"})
    if role == "admin" and (sphere == "biz" or sphere is None):
        personal = "Мои лиды: ВКЛ" if can_receive_leads else "Мои лиды: ВЫКЛ"
        items.append({"text": personal, "icon": "📥", "href": "#"})
    return items


def _session_view_model():
    role = session.get("role", "")
    sphere = session.get("sphere") or "—"
    return {
        "menu": get_menu_items(role, session.get("sphere"), session.get("can_receive_leads", 0)),
        "fio": session.get("fio", ""),
        "role": role,
        "sphere": sphere,
    }


def _parse_dt(value: str):
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


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
    is_local = "localhost" in (request.host or "") or "127.0.0.1" in (request.host or "")
    users = []
    if is_local:
        rows = execute_query(
            "SELECT user_id, COALESCE(fio,''), role, COALESCE(sphere,'') FROM users ORDER BY role, user_id",
            (),
            fetchall=True,
        )
        users = [{"user_id": r[0], "fio": r[1] or f"ID{r[0]}", "role": r[2], "sphere": r[3]} for r in (rows or [])]
    return render_template(
        "login.html", bot_username=BOT_USERNAME, auth_url=auth_url, dev_users=users, is_local=is_local
    )


@app.route("/auth/dev", methods=["POST"])
def auth_dev():
    if "localhost" not in (request.host or "") and "127.0.0.1" not in (request.host or ""):
        return redirect(url_for("login") + "?error=invalid")
    user_id = request.form.get("user_id")
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
    session["fio"] = fio or f"User {user_id}"
    session["can_receive_leads"] = can_receive_leads
    return redirect(url_for("dashboard"))


@app.route("/auth/telegram", methods=["GET"])
def auth_telegram():
    api_token, _ = _get_config()
    data = {k: v for k, v in request.args.items()}
    if not data or not verify_telegram_login(data, api_token):
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
@login_required
def dashboard():
    return render_template("dashboard.html", **_session_view_model())


@app.route("/online-booking")
@login_required
@booking_access_required
def online_booking():
    return render_template("online_booking.html", **_session_view_model())


@app.route("/online-booking/dictionaries")
@login_required
@booking_access_required
def booking_dictionaries():
    return render_template("online_booking_dictionaries.html", **_session_view_model())


@app.route("/online-booking/journal")
@login_required
@booking_access_required
def booking_journal():
    return render_template("online_booking_journal.html", **_session_view_model())


@app.get("/web-api/booking/queue")
@login_required
@booking_access_required
def api_booking_queue():
    rows = execute_query(
        """SELECT phone, COALESCE(name, phone), manager_id, last_touch
           FROM leads
           WHERE status IN ('paid','Оплатил','Купил')
             AND (phone NOT IN (SELECT patient_phone FROM medical_appointments WHERE status = 'booked'))
           ORDER BY COALESCE(last_touch,'') DESC
           LIMIT 200""",
        fetchall=True,
    )
    return jsonify(
        [
            {
                "phone": r[0],
                "name": r[1],
                "responsible_manager_id": r[2],
                "paid_at": r[3],
            }
            for r in (rows or [])
        ]
    )


@app.get("/web-api/booking/directions")
@login_required
@booking_access_required
def api_booking_directions():
    rows = execute_query(
        "SELECT id, name, duration_min, is_active FROM medical_directions ORDER BY id DESC",
        fetchall=True,
    )
    return jsonify(
        [
            {"id": r[0], "name": r[1], "duration_min": r[2], "is_active": bool(r[3])}
            for r in (rows or [])
        ]
    )


@app.post("/web-api/booking/directions")
@login_required
@booking_access_required
def api_booking_add_direction():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    duration_min = int(body.get("duration_min") or 30)
    if not name:
        return jsonify({"detail": "name is required"}), 400
    try:
        execute_query(
            "INSERT INTO medical_directions (name, duration_min, is_active) VALUES (?, ?, 1)",
            (name, duration_min),
        )
    except sqlite3.IntegrityError:
        return jsonify({"detail": "Direction already exists"}), 409
    return jsonify({"ok": True}), 201


@app.get("/web-api/booking/specialists")
@login_required
@booking_access_required
def api_booking_specialists():
    rows = execute_query(
        """SELECT s.id, s.full_name, s.direction_id, d.name, s.phone, s.is_active
           FROM medical_specialists s
           LEFT JOIN medical_directions d ON d.id = s.direction_id
           ORDER BY s.id DESC""",
        fetchall=True,
    )
    return jsonify(
        [
            {
                "id": r[0],
                "full_name": r[1],
                "direction_id": r[2],
                "direction_name": r[3],
                "phone": r[4],
                "is_active": bool(r[5]),
            }
            for r in (rows or [])
        ]
    )


@app.post("/web-api/booking/specialists")
@login_required
@booking_access_required
def api_booking_add_specialist():
    body = request.get_json(silent=True) or {}
    full_name = str(body.get("full_name", "")).strip()
    direction_id = int(body.get("direction_id") or 0)
    phone = str(body.get("phone", "")).strip()
    if not full_name or not direction_id:
        return jsonify({"detail": "full_name and direction_id are required"}), 400
    execute_query(
        "INSERT INTO medical_specialists (full_name, direction_id, phone, is_active) VALUES (?, ?, ?, 1)",
        (full_name, direction_id, phone),
    )
    return jsonify({"ok": True}), 201


@app.get("/web-api/booking/appointments")
@login_required
@booking_access_required
def api_booking_appointments():
    date_s = request.args.get("date", "").strip()
    specialist_id = request.args.get("specialist_id", "").strip()
    q = """SELECT a.id, a.patient_name, a.patient_phone, a.start_at, a.end_at, a.status,
                  a.responsible_manager_id, d.name, s.full_name, a.comment
           FROM medical_appointments a
           LEFT JOIN medical_directions d ON d.id = a.direction_id
           LEFT JOIN medical_specialists s ON s.id = a.specialist_id
           WHERE 1=1"""
    params = []
    if date_s:
        q += " AND substr(a.start_at, 1, 10) = ?"
        params.append(date_s)
    if specialist_id:
        q += " AND a.specialist_id = ?"
        params.append(int(specialist_id))
    q += " ORDER BY a.start_at ASC"
    rows = execute_query(q, tuple(params), fetchall=True)
    return jsonify(
        [
            {
                "id": r[0],
                "patient_name": r[1],
                "patient_phone": r[2],
                "start_at": r[3],
                "end_at": r[4],
                "status": r[5],
                "responsible_manager_id": r[6],
                "direction_name": r[7],
                "specialist_name": r[8],
                "comment": r[9],
            }
            for r in (rows or [])
        ]
    )


@app.post("/web-api/booking/appointments")
@login_required
@booking_access_required
def api_booking_create_appointment():
    body = request.get_json(silent=True) or {}
    patient_name = str(body.get("patient_name", "")).strip()
    patient_phone = str(body.get("patient_phone", "")).strip()
    lead_phone = str(body.get("lead_phone", patient_phone)).strip()
    direction_id = int(body.get("direction_id") or 0)
    specialist_id = int(body.get("specialist_id") or 0)
    start_at = str(body.get("start_at", "")).strip()
    responsible_manager_id = body.get("responsible_manager_id")
    comment = str(body.get("comment", "")).strip()
    if not all([patient_name, patient_phone, direction_id, specialist_id, start_at]):
        return jsonify({"detail": "Missing required fields"}), 400
    direction = execute_query(
        "SELECT duration_min FROM medical_directions WHERE id = ?",
        (direction_id,),
        fetchone=True,
    )
    if not direction:
        return jsonify({"detail": "Direction not found"}), 404
    duration_min = int(direction[0] or 30)
    dt_start = _parse_dt(start_at)
    dt_end = dt_start.replace(second=0, microsecond=0)
    dt_end = dt_end.timestamp() + duration_min * 60
    end_at = datetime.fromtimestamp(dt_end).strftime("%Y-%m-%d %H:%M")
    overlap = execute_query(
        """SELECT 1 FROM medical_appointments
           WHERE specialist_id = ? AND status = 'booked'
             AND NOT (end_at <= ? OR start_at >= ?)
           LIMIT 1""",
        (specialist_id, start_at, end_at),
        fetchone=True,
    )
    if overlap:
        return jsonify({"detail": "Slot already occupied"}), 409
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        """INSERT INTO medical_appointments
           (lead_phone, patient_name, patient_phone, direction_id, specialist_id, start_at, end_at,
            status, responsible_manager_id, created_by_user_id, comment, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'booked', ?, ?, ?, ?, ?)""",
        (
            lead_phone,
            patient_name,
            patient_phone,
            direction_id,
            specialist_id,
            start_at,
            end_at,
            responsible_manager_id,
            session.get("user_id"),
            comment,
            now_s,
            now_s,
        ),
    )
    return jsonify({"ok": True}), 201


@app.patch("/web-api/booking/appointments/<int:appointment_id>/status")
@login_required
@booking_access_required
def api_booking_update_status(appointment_id):
    body = request.get_json(silent=True) or {}
    status = str(body.get("status", "")).strip()
    if status not in {"booked", "completed", "no_show", "cancelled"}:
        return jsonify({"detail": "Invalid status"}), 400
    execute_query(
        "UPDATE medical_appointments SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), appointment_id),
    )
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "80"))
    app.run(host="0.0.0.0", port=port, debug=False)
