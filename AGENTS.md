# Agent: CRM Bot (Metodi)

# AGENTS.md — METODI CRM (crm-bot)

## Purpose
Telegram CRM for leads (Medicine + Business) with WhatsApp via Green API, manager relay, optional Flask web UI, SQLite, Amvera deploy.

## Stack
Python 3.11, aiogram 3, Flask, SQLite, Green API, optional Telethon + Google Sheets.

## Run
```bash
pip install -r requirements.txt
copy .env.example .env
python main.py          # bot only
python run_all.py       # bot + web
```

## Key files
- `main.py` — bot handlers (~4k lines)
- `db.py` — SQLite schema
- `config.py` — env config
- `web/app.py` — Flask dashboard

## Agent rules
- Monolith in `main.py`; DB migrations in `db.init_db()`
- Russian UI strings; roles: owner/admin/manager; spheres med/biz
- Secrets via env only; `CRM_DB_PATH=/data/crm_base.db` on Amvera
- See DEPLOY.md, TELEGRAM_LEADS.md, GOOGLE_SHEET_SETUP.md
