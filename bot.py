# bot.py
from __future__ import annotations

import os
import time
import json
import asyncio
import logging
import threading
from typing import Optional

from flask import Flask, request, abort

from telegram import Update, Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import RetryAfter

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # например: https://my-telegram-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# -------------------- Flask (WSGI) --------------------
app_flask = Flask(__name__)

# Глобальная ссылка на приложение PTB
app_tg: Optional[Application] = None


# -------------------- handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# -------------------- healthcheck --------------------
@app_flask.route("/", methods=["GET"])
def health() -> tuple[str, int]:
    return "OK", 200


# -------------------- Webhook endpoint --------------------
@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # Проверяем секретный заголовок от Telegram
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    # PTB ещё не успел подняться
    if app_tg is None:
        log.error("Webhook got request, but bot is not ready yet")
        return "ok", 200

    try:
        data = request.get_json(force=True, silent=False)
        if not data:
            return "ok", 200

        # Превращаем JSON -> Update и кидаем в очередь PTB
        update = Update.de_json(data, app_tg.bot)
        app_tg.update_queue.put_nowait(update)
    except Exception:
        log.exception("Error in webhook_handler")
    return "ok", 200


# -------------------- запуск PTB --------------------
def _run_bot() -> None:
    """Поднимает PTB и слушает порт. Вебхук ставится отдельно."""
    global app_tg

    log.info("Starting PTB Application...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Делаем приложение доступным для Flask webhook
    app_tg = application

    # IMPORTANT: не передаем webhook_url/secret_token здесь,
    # чтобы не плодить setWebhook и не ловить RetryAfter.
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=WEBHOOK_PATH,
        # PTB сам отдаст 200 на GET / и повесит HTTP сервер;
        # сам вебхук мы выставим отдельно ниже.
    )


# -------------------- отдельная установка вебхука --------------------
async def _set_webhook_once():
    """Ставит webhook c повторами на случай RetryAfter."""
    bot = Bot(BOT_TOKEN)

    for attempt in range(5):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            log.info("Webhook set to %s", WEBHOOK_URL)
            return
        except RetryAfter as e:
            wait_s = int(getattr(e, "retry_after", 1)) + 1
            log.warning(
                "setWebhook rate-limited. Retry in %s s (attempt %s/5)",
                wait_s, attempt + 1
            )
            await asyncio.sleep(wait_s)
        except Exception:
            log.exception("Failed to set webhook (attempt %s/5)", attempt + 1)
            await asyncio.sleep(2)

    log.error("Giving up setting webhook after 5 attempts")


def _set_webhook_later():
    # Чуть ждём, чтобы HTTP-сервер PTB начал слушать порт
    time.sleep(2)
    try:
        asyncio.run(_set_webhook_once())
    except Exception:
        log.exception("set_webhook_later crashed")


# -------------------- entrypoint --------------------
if __name__ == "__main__":
    # 1) поднимаем PTB в фоне
    threading.Thread(target=_run_bot, daemon=True).start()
    # 2) в отдельном потоке выставляем webhook (с ретраями)
    threading.Thread(target=_set_webhook_later, daemon=True).start()
