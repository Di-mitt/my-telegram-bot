# bot.py
from __future__ import annotations

import os
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ContextTypes, filters,
)

# --- env ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # пример: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# --- Flask app для gunicorn ---
app_flask = Flask(__name__)

# Создадим позже (ниже в __main__)
app_tg: Application | None = None


# --- handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# --- PTB startup: выставляем вебхук ---
async def on_startup(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
    )


# --- HTTP-маршрут для Telegram ---
@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # Проверяем секретный заголовок
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    # Отдаём апдейты в PTB
    data = request.get_json(force=True)
    if app_tg is not None:
        app_tg.update_queue.put_nowait(Update.de_json(data, app_tg.bot))
    return "ok"


# --- Entry point ---
if __name__ == "__main__":
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    app_tg.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        on_startup=[on_startup],
    )
