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
BOT_TOKEN = os.getenv("BOT_TOKEN")                      # токен бота
APP_URL = os.getenv("APP_URL")                          # напр.: https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = f"{APP_URL}{WEBHOOK_PATH}"

# ─────────────── ГЛОБАЛЬНЫЕ ───────────────
app_flask = Flask(__name__)                # WSGI-приложение (его запускает gunicorn)
app_tg: Optional[Application] = None       # PTB-приложение (мы поднимем в отдельном потоке)

_pending_lock = threading.Lock()
_pending_updates: Deque[dict] = deque()    # буфер апдейтов, пока бот не готов


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
    # 1) проверяем секретный заголовок
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    # 2) читаем JSON от Telegram
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Cannot parse webhook JSON")
        return "ok", 200

    if not data:
        return "ok", 200

    # 3) если PTB ещё не поднят — буферизуем
    if app_tg is None:
        with _pending_lock:
            _pending_updates.append(data)
        log.warning("Buffered update while bot not ready (queue=%d)", len(_pending_updates))
        return "ok", 200

    # 4) если PTB уже работает — сразу в очередь PTB
    try:
        upd = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Failed to enqueue live update")
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
        # даём gunicorn/Flask поднять порт
        time.sleep(2)
        try:
            asyncio.run(_set_webhook_with_retries())
        except Exception:
            log.exception("Webhook setter crashed")
    threading.Thread(target=_runner, name="webhook-setter", daemon=True).start()


# ─────────────── ПОДЪЁМ PTB, СЛИВ БУФЕРА ───────────────
def _start_ptb_thread() -> None:
    def _runner():
        global app_tg
        try:
            log.info("Starting PTB Application...")
            application = ApplicationBuilder().token(BOT_TOKEN).build()

            application.add_handler(CommandHandler("start", start_cmd))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

            # Делаем доступным для Flask
            app_tg = application

            # Сливаем то, что пришло ранним вебхуком
            with _pending_lock:
                while _pending_updates:
                    data = _pending_updates.popleft()
                    try:
                        upd = Update.de_json(data, app_tg.bot)
                        app_tg.update_queue.put_nowait(upd)
                    except Exception:
                        log.exception("Failed to enqueue buffered update")

            # Запускаем HTTP приёмник PTB (без setWebhook — он ставится отдельно)
            application.run_webhook(
                listen="0.0.0.0",
                port=int(os.environ.get("PORT", 10000)),
                url_path=WEBHOOK_PATH,
            )
        except Exception:
            log.exception("PTB application crashed")
    threading.Thread(target=_runner, name="ptb-runner", daemon=True).start()


# ─────────────── СТАРТ ПРИ ИМПОРТЕ (для gunicorn) ───────────────
_start_ptb_thread()
_start_webhook_setter_thread()
