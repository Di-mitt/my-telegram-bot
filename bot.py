# bot.py
from __future__ import annotations

import os
import json
import logging
import threading
import asyncio
from typing import Optional

from flask import Flask, request, abort
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ────────────────────────────── ЛОГИ ──────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ─────────────────────── ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ─────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")  # например: 123456:AA... 
APP_URL = os.getenv("APP_URL")      # например: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ────────────────────────── ГЛОБАЛЬНЫЕ ────────────────────────────
app_flask = Flask(__name__)                 # WSGI-приложение для Render
app_tg: Optional[Application] = None        # PTB Application (создадим в фоне)

_bot_lock = threading.Lock()                # чтобы не запустить дважды
_bot_started = False
_bot_ready = threading.Event()              # становится True, когда бот готов

# ─────────────────────────── HANDLERS ─────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("Привет! Я проснулся и на связи 🤖")
    except Exception:
        log.exception("Error in /start handler")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.message and update.message.text:
            await update.message.reply_text(f"Вы написали: {update.message.text}")
    except Exception:
        log.exception("Error in echo handler")

# ───────────────────── ЗАПУСК PTB В ФОНОВОМ ПОТОКЕ ─────────────────
async def _async_start_bot() -> None:
    """Создаём и запускаем PTB Application в отдельном asyncio-цикле."""
    global app_tg

    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # initialize/start запускают внутренние таски, очередь апдейтов и т.д.
    await app_tg.initialize()
    await app_tg.start()

    # ВАЖНО: помечаем бота готовым ДО установки вебхука,
    # чтобы первый запрос не пришёл "слишком рано".
    _bot_ready.set()
    log.info("Bot core is ready, setting webhook...")

    # Ставим (пере)вебхук
    await app_tg.bot.delete_webhook(drop_pending_updates=True)
    await app_tg.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook set to %s", WEBHOOK_URL)

    # Держим цикл живым
    while True:
        await asyncio.sleep(3600)

def _thread_target() -> None:
    """Точка входа фонового треда: свой event loop для PTB."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(_async_start_bot())
        loop.run_forever()
    except Exception:
        log.exception("Background loop crashed")

def ensure_bot_started() -> None:
    """Запускаем фонового бота один раз при первом обращении/импорте."""
    global _bot_started
    if _bot_started:
        return
    with _bot_lock:
        if _bot_started:
            return
        t = threading.Thread(target=_thread_target, name="ptb-thread", daemon=True)
        t.start()
        _bot_started = True
        log.info("PTB background thread started")

# Стартуем бота как можно раньше (чтобы к первому запросу уже успел подняться)
ensure_bot_started()

# ─────────────────────────── FLASK ROUTES ─────────────────────────
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    """Приём апдейтов от Telegram с максимальной устойчивостью."""
    ensure_bot_started()

    # Проверяем секрет
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    # Ждём готовности бота (увеличено время ожидания)
    if not _bot_ready.wait(timeout=30):
        log.error("Webhook got request, but bot is not ready yet")
        return "ok", 200

    try:
        data = request.get_json(force=True, silent=False)
        if not data or not app_tg:
            return "ok", 200

        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
        return "ok", 200
    except Exception:
        log.exception("Error in webhook_handler")
        return "ok", 200
