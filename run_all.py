# Единая точка входа для Amvera: веб-интерфейс + Telegram-бот в одном процессе.
# Запуск: python run_all.py
# На Amvera задай переменную PORT (если платформа её передаёт) и CRM_DB_PATH (например /data/crm_base.db).

import os
import sys
import threading
import asyncio

def run_bot():
    """Запуск бота в отдельном потоке (свой event loop)."""
    try:
        import main as bot_main
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_main.main())
    except Exception as e:
        print(f"[run_all] Bot thread error: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    from web.app import app
    print(f"[run_all] Web on 0.0.0.0:{port}, bot in background", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
