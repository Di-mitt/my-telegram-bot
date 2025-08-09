# bot.py
from __future__ import annotations

import os
import time
import asyncio
import logging
import threading
from typing import Optional, Deque
from collections import deque

from flask import Flask, request, abort
from telegram import Update, Bot
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from telegram.error import RetryAfter

# ─────────────── ЛОГИ ───────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ─────────────── ОКРУЖЕНИЕ ───────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")                           # напр.: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = f"{APP_URL}{WEBHOOK_PATH}"

# ─────────────── ГЛОБАЛЬНЫЕ ───────────────
app_flask = Flask(__name__)
app_tg: Optional[Application] = None                 # PTB-приложение (инициализируем в фоне)

_pending_lock = threading.Lock()
_pending_updates: Deque[dict] = deque()              # буфер апдейтов, пока бот не готов
_stop_event = threading.Event()                      # на случай корректного завершения

# ─────────────── HANDLERS ───────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Бот на Render и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# ─────────────── HEALTH ───────────────
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200

# ─────────────── WEBHOOK (Flask) ───────────────
@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # 1) секретный заголовок от Telegram
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    # 2) читаем JSON
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Cannot parse webhook JSON")
        return "ok", 200
    if not data:
        return "ok", 200

    # 3) если PTB уже поднят — сразу в очередь PTB (без буфера)
    if app_tg is not None:
        try:
            upd = Update.de_json(data, app_tg.bot)
            app_tg.update_queue.put_nowait(upd)
            return "ok", 200
        except Exception:
            log.exception("Failed to enqueue live update")
            return "ok", 200

    # 4) иначе — буферизуем (и флашер сам перельёт позже)
    with _pending_lock:
        _pending_updates.append(data)
        q = len(_pending_updates)
    if q % 5 == 0:  # пореже спамить логи
        log.warning("Buffered updates while bot not ready (queue=%d)", q)
    return "ok", 200

# ─────────────── УСТАНОВКА ВЕБХУКА (с ретраями) ───────────────
async def _set_webhook_with_retries() -> None:
    bot = Bot(BOT_TOKEN)
    for attempt in range(5):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            log.info("Webhook set to %s", WEBHOOK_URL)
            return
        except RetryAfter as e:
            wait_s = int(getattr(e, "retry_after", 1)) + 1
            log.warning("Rate limited on setWebhook. Retry in %s s (attempt %s/5)", wait_s, attempt + 1)
            await asyncio.sleep(wait_s)
        except Exception:
            log.exception("Failed to set webhook (attempt %s/5)", attempt + 1)
            await asyncio.sleep(2)
    log.error("Giving up setting webhook after 5 attempts")

def _start_webhook_setter_thread() -> None:
    def _runner():
        # дадим gunicorn/Flask поднять порт
        time.sleep(2)
        try:
            asyncio.run(_set_webhook_with_retries())
        except Exception:
            log.exception("Webhook setter crashed")
    threading.Thread(target=_runner, name="webhook-setter", daemon=True).start()

# ─────────────── ПОДЪЁМ PTB (без run_webhook/polling) ───────────────
async def _async_ptb_main() -> None:
    """Создаём Application, запускаем его и держим цикл живым."""
    global app_tg

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    await application.initialize()
    await application.start()

    app_tg = application  # теперь можно сливать буфер

    # держим живым; PTB сам слушает update_queue
    try:
        while not _stop_event.is_set():
            await asyncio.sleep(3600)
    finally:
        await application.stop()
        await application.shutdown()

def _start_ptb_thread() -> None:
    def _runner():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_async_ptb_main())
        except Exception:
            log.exception("PTB application crashed")
    threading.Thread(target=_runner, name="ptb-runner", daemon=True).start()

# ─────────────── ПОСТОЯННЫЙ ФЛАШЕР БУФЕРА ───────────────
def _start_buffer_flusher() -> None:
    """Фоновый поток: как только app_tg готов, перелей всё из буфера.
       Работает постоянно: на случай кратковременных рестартов PTB.
    """
    def _runner():
        while not _stop_event.is_set():
            if app_tg is not None:
                try:
                    # быстро переливаем всё, что накопилось
                    batch: list[dict] = []
                    with _pending_lock:
                        while _pending_updates:
                            batch.append(_pending_updates.popleft())
                    for data in batch:
                        try:
                            upd = Update.de_json(data, app_tg.bot)
                            app_tg.update_queue.put_nowait(upd)
                        except Exception:
                            log.exception("Failed to enqueue buffered update")
                except Exception:
                    log.exception("Buffer flusher loop error")
            time.sleep(0.2)  # 200 мс между проходами
    threading.Thread(target=_runner, name="buffer-flusher", daemon=True).start()

# ─────────────── СТАРТ ПРИ ИМПОРТЕ ───────────────
_start_ptb_thread()
_start_webhook_setter_thread()
_start_buffer_flusher()
