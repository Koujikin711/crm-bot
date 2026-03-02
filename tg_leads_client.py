# -*- coding: utf-8 -*-
"""
Клиент Telethon для аккаунта, на который приходят лиды (например +992877631000).
Читает входящие личные сообщения, передаёт их в CRM; отправляет ответы менеджеров обратно в Telegram.
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_client = None

def is_available():
    return _client is not None and _client.is_connected()

async def send_message_to_lead(peer_id: int, text: str):
    """Отправить текстовое сообщение лиду с аккаунта (от имени номера +992...)."""
    if not _client or not _client.is_connected():
        logger.warning("tg_leads: client not connected, cannot send to %s", peer_id)
        return False
    try:
        await _client.send_message(peer_id, text)
        return True
    except Exception as e:
        logger.exception("tg_leads send_message to %s: %s", peer_id, e)
        return False

async def send_media_to_lead(peer_id: int, file_path: str, caption: str = None, is_photo: bool = True):
    """Отправить файл (фото/документ/голос) лиду."""
    if not _client or not _client.is_connected():
        logger.warning("tg_leads: client not connected, cannot send file to %s", peer_id)
        return False
    try:
        if is_photo:
            await _client.send_file(peer_id, file_path, caption=caption)
        else:
            await _client.send_file(peer_id, file_path, caption=caption, force_document=not is_photo)
        return True
    except Exception as e:
        logger.exception("tg_leads send_file to %s: %s", peer_id, e)
        return False

async def send_file_bytes_to_lead(peer_id: int, file_bytes: bytes, file_name: str = "file", caption: str = None, as_photo: bool = False):
    """Отправить файл из байтов (для ответа менеджера с фото/голосом и т.д.)."""
    if not _client or not _client.is_connected():
        logger.warning("tg_leads: client not connected, cannot send file to %s", peer_id)
        return False
    try:
        await _client.send_file(peer_id, file_bytes, caption=caption, force_document=not as_photo)
        return True
    except Exception as e:
        logger.exception("tg_leads send_file_bytes to %s: %s", peer_id, e)
        return False

async def run_client(phone: str, api_id: str, api_hash: str, session_path: str, message_handler):
    """
    Запуск клиента Telethon. message_handler(peer_id, name, text, has_media) — вызывается при каждом
    входящем личном сообщении; peer_id — Telegram user id отправителя.
    """
    global _client
    try:
        from telethon import TelegramClient
        from telethon.events import NewMessage
        from telethon.tl.types import PeerUser
    except ImportError:
        logger.error("telethon not installed: pip install telethon")
        return

    api_id_int = int(api_id) if api_id.isdigit() else None
    if not api_id_int or not api_hash:
        logger.warning("tg_leads: TELEGRAM_API_ID or TELEGRAM_API_HASH not set, skipping")
        return

    _client = TelegramClient(session_path, api_id_int, api_hash)

    @_client.on(NewMessage(incoming=True))
    async def on_message(event):
        if not event.is_private or not event.message:
            return
        peer = event.message.peer_id
        if not isinstance(peer, PeerUser):
            return
        peer_id = peer.user_id
        sender = await event.get_sender()
        name = (getattr(sender, "first_name", "") or "") + " " + (getattr(sender, "last_name", "") or "")
        name = name.strip() or "Клиент"
        text = event.message.text or ""
        if event.message.media and not text:
            text = "[медиа]"
        try:
            await message_handler(peer_id, name, text, bool(event.message.media))
        except Exception as e:
            logger.exception("tg_leads message_handler: %s", e)

    try:
        await _client.start(phone=phone or None)
        logger.info("tg_leads: client started for %s", phone or session_path)
        await _client.run_until_disconnected()
    except Exception as e:
        logger.exception("tg_leads run: %s", e)
    finally:
        _client = None
