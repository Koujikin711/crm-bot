"""
API для мобильного приложения METODI CRM.
Использует ту же SQLite БД, что и бот.

Запуск: uvicorn api_server:app --host 0.0.0.0 --port 8000

Асинхронные хендлеры (`async def`): блокирующий sqlite3 выполняется в пуле потоков
(`asyncio.to_thread`), чтобы не блокировать event loop под нагрузкой.
"""
import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional

try:
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm_base.db"))


def execute_query(query: str, params=(), fetchone=False, fetchall=False):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute(query, params)
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        conn.commit()


def get_user_role(user_id: int):
    row = execute_query("SELECT role FROM users WHERE user_id = ?", (str(user_id),), fetchone=True)
    return row[0] if row else None


async def execute_query_async(
    query: str, params: tuple = (), fetchone: bool = False, fetchall: bool = False
) -> Any:
    """Не блокирует event loop (SQLite синхронный)."""
    return await asyncio.to_thread(execute_query, query, params, fetchone, fetchall)


async def get_user_role_async(user_id: int) -> Optional[str]:
    return await asyncio.to_thread(get_user_role, user_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="METODI CRM API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Liveness/readiness для балансировщиков и health-check UI."""
    return {"status": "ok", "service": "metodi-crm-api"}


@app.get("/")
async def root():
    return {"name": "METODI CRM API", "docs": "/docs", "health": "/health"}


@app.get("/api/me")
async def api_me(user_id: str = Query(..., alias="user_id")):
    """Проверка доступа: user_id — Telegram ID пользователя."""
    try:
        uid = int(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")
    role = await get_user_role_async(uid)
    if not role:
        raise HTTPException(status_code=401, detail="User not found")
    return {"user_id": uid, "role": role}


@app.get("/api/leads")
async def api_leads(user_id: str = Query(..., alias="user_id")):
    """Список лидов: для менеджера — свои, для владельца/админа — все по направлению."""
    try:
        uid = int(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")
    role = await get_user_role_async(uid)
    if not role:
        raise HTTPException(status_code=401, detail="User not found")

    if role == "owner":
        rows = await execute_query_async(
            """SELECT l.phone, l.name, l.status, l.manager_id, l.touches, l.last_touch, l.direction,
                      u.fio FROM leads l LEFT JOIN users u ON l.manager_id = u.user_id
               ORDER BY l.last_touch DESC""",
            (),
            fetchall=True,
        )
    else:
        rows = await execute_query_async(
            """SELECT l.phone, l.name, l.status, l.manager_id, l.touches, l.last_touch, l.direction,
                      u.fio FROM leads l LEFT JOIN users u ON l.manager_id = u.user_id
               WHERE l.manager_id = ? ORDER BY l.last_touch DESC""",
            (uid,),
            fetchall=True,
        )

    out = []
    for r in rows:
        phone, name, status, mgr_id, touches, last_touch, direction, mgr_fio = r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]
        out.append({
            "phone": phone,
            "name": name or phone,
            "status": status or "",
            "manager_id": mgr_id,
            "manager_name": mgr_fio or (str(mgr_id) if mgr_id else None),
            "touches": touches or 0,
            "last_touch": last_touch[:16] if last_touch and len(last_touch) >= 16 else last_touch,
            "direction": direction or "biz",
        })
    return out


@app.get("/api/leads/{phone:path}")
async def api_lead(phone: str, user_id: str = Query(..., alias="user_id")):
    """Один лид по номеру телефона."""
    try:
        uid = int(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")
    role = await get_user_role_async(uid)
    if not role:
        raise HTTPException(status_code=401, detail="User not found")

    row = await execute_query_async(
        """SELECT l.phone, l.name, l.status, l.manager_id, l.touches, l.last_touch, l.direction,
                  u.fio FROM leads l LEFT JOIN users u ON l.manager_id = u.user_id
           WHERE l.phone = ?""",
        (phone,),
        fetchone=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    if role != "owner" and row[3] != uid:
        raise HTTPException(status_code=403, detail="No access")

    return {
        "phone": row[0],
        "name": row[1] or row[0],
        "status": row[2] or "",
        "manager_id": row[3],
        "manager_name": row[7] or (str(row[3]) if row[3] else None),
        "touches": row[4] or 0,
        "last_touch": row[5][:16] if row[5] and len(row[5]) >= 16 else row[5],
        "direction": row[6] or "biz",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
