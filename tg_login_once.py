# -*- coding: utf-8 -*-
"""
Однократный вход в Telegram-аккаунт для лидов.
Запусти ЭТОТ СКРИПТ НА СВОЁМ КОМПЬЮТЕРЕ (не на Amvera): в консоли попросят код из Telegram — введи его.
После успешного входа появится файл tg_leads_session.session — его нужно загрузить на Amvera (см. TELEGRAM_LEADS.md).
"""
import asyncio
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

PHONE = os.environ.get("TELEGRAM_LEADS_PHONE", "").strip()
API_ID = os.environ.get("TELEGRAM_API_ID", "").strip()
API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
SESSION_PATH = os.environ.get("TELEGRAM_SESSION_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_leads_session"))

async def main():
    if not API_ID or not API_HASH:
        print("Задай в .env: TELEGRAM_API_ID, TELEGRAM_API_HASH (и TELEGRAM_LEADS_PHONE)")
        sys.exit(1)
    try:
        from telethon import TelegramClient
    except ImportError:
        print("Установи: pip install telethon")
        sys.exit(1)

    client = TelegramClient(SESSION_PATH, int(API_ID), API_HASH)
    await client.start(phone=PHONE or None)
    print("Вход выполнен. Сессия сохранена в:", SESSION_PATH + ".session")
    print("Файл", repr(SESSION_PATH + ".session"), "— загрузи его на Amvera (секретный файл или том).")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
