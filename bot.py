# bot.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional

from flask import Flask, Request, abort, request
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- logging ----------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# --------------- env ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # обязательно
APP_URL = os.getenv("APP_URL")      # например https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# --------------- globals ----------------
app_flask = Flask(__name__)
app_tg: Optional[Application] = None

_ptb_ready = threading.Event()   # ставим, когда PTB запущен
_loop: Optional[asyncio.AbstractEventLoop] = None


# --------------- handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# --------------- PTB runner ----------------
async def _ensure_webhook(bot):
    """Ставит вебхук в фоне (не блокирует готовность)."""
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        log.exception("Failed to set webhook")


def _ptb_thread():
    """Запуск PTB ядра в отдельном потоке со своим loop."""
    global app_tg, _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    app_tg = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    async def _start():
        # initialize + start, после этого очередь уже принимает апдейты
        await app_tg.initialize()
        await app_tg.start()
        _ptb_ready.set()  # <<< отмечаем "готово" сразу после старта
        # вебхук ставим в фоне, чтобы не блокировать
        asyncio.create_task(_ensure_webhook(app_tg.bot))

        # держим приложение
        await asyncio.Event().wait()

    try:
        _loop.run_until_complete(_start())
    except Exception:
        log.exception("PTB runner crashed")


def _ensure_ptb_started():
    """Гарантированно запускаем поток с PTB (один раз)."""
    if not _ptb_ready.is_set():
        t = threading.Thread(target=_ptb_thread, name="ptb-runner", daemon=True)
        t.start()


# --------------- Flask routes ----------------
@app_flask.get("/")
def health():
    _ensure_ptb_started()
    return "OK", 200


@app_flask.post(WEBHOOK_PATH)
def webhook_handler():
    # проверяем секрет
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    # гарантируем запуск PTB
    _ensure_ptb_started()

    # ждём готовности PTB максимум 5 секунд
    if not _ptb_ready.wait(timeout=5):
        log.warning("Got update but PTB not ready yet — dropping safely")
        return "ok", 200

    try:
        data: dict = request.get_json(force=True, silent=False)
        # полезно при отладке — видеть «сырые» апдейты
        log.info("Webhook JSON: %s", json.dumps(data, ensure_ascii=False))

        # передаём апдейт в PTB
        upd = Update.de_json(data, app_tg.bot)  # type: ignore[arg-type]
        app_tg.update_queue.put_nowait(upd)     # type: ignore[union-attr]
        return "ok", 200
    except Exception:
        log.exception("Error while handling webhook")
        return "ok", 200


# --------------- local run ----------------
if __name__ == "__main__":
    # локальный запуск — для Render это не используется
    _ensure_ptb_started()
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
