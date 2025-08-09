# bot.py
from __future__ import annotations

import os
import time
import asyncio
import logging
import threading
from typing import Optional

from flask import Flask, request, abort
from telegram import Update, Bot
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from telegram.error import RetryAfter

# ── Логи ───────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ── Переменные окружения (Render → Environment) ────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")                         # токен @BotFather
APP_URL = os.getenv("APP_URL")                             # напр.: https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = f"{APP_URL}{WEBHOOK_PATH}"

# ── Flask (WSGI для Render/gunicorn) ───────────────────────────────
app_flask = Flask(__name__)

# Глобальная ссылка на PTB-приложение — нужна Flask-роуту
app_tg: Optional[Application] = None


# ── Telegram handlers ──────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Бот живёт на Render и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# ── Healthcheck ────────────────────────────────────────────────────
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


# ── Webhook endpoint (приём апдейтов от Telegram) ──────────────────
@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # 1) проверяем секретный заголовок
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    # 2) если PTB ещё не поднят — отвечаем 200, TG сам ретрайнет
    if app_tg is None:
        log.warning("Webhook came, but Application not ready yet")
        return "ok", 200

    # 3) кладём апдейт в очередь PTB
    try:
        data = request.get_json(force=True, silent=False)
        if not data:
            return "ok", 200
        upd = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(upd)
    except Exception:
        log.exception("Error in webhook_handler")
    return "ok", 200


# ── Фоновый запуск PTB и установка вебхука ─────────────────────────
async def _set_webhook_with_retries() -> None:
    """Ставит webhook через Bot API, с повторами при Rate Limit."""
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


def _start_ptb_thread() -> None:
    def _runner():
        global app_tg
        try:
            log.info("Starting PTB Application...")
            application = ApplicationBuilder().token(BOT_TOKEN).build()
            application.add_handler(CommandHandler("start", start_cmd))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
            app_tg = application

            # поднимаем встроенный HTTP (для очереди/внутренних задач PTB)
            application.run_webhook(
                listen="0.0.0.0",
                port=int(os.environ.get("PORT", 10000)),
                url_path=WEBHOOK_PATH,
                # НЕ передаём webhook_url/secret_token здесь — мы их ставим отдельной корутиной,
                # чтобы контролировать ретраи и не плодить setWebhook на каждый рестарт.
            )
        except Exception:
            log.exception("PTB application crashed")
    threading.Thread(target=_runner, name="ptb-runner", daemon=True).start()


# Стартуем всё при импорте модуля (когда gunicorn загружает app_flask)
_start_ptb_thread()
_start_webhook_setter_thread()
