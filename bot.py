# bot.py
from __future__ import annotations

import os
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

# --- env ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # пример: https://my-telegram-bot-xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not BOT_TOKEN or not APP_URL or not WEBHOOK_SECRET:
    raise RuntimeError("Set env vars BOT_TOKEN, APP_URL and WEBHOOK_SECRET")

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
    await update.message.reply_text(update.message.text)


# --- Flask routes ---
@app_flask.route("/")
def index():
    return "Бот запущен!", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.method == "POST":
        if app_tg is None:
            abort(500)
        try:
            update = Update.de_json(request.get_json(force=True), app_tg.bot)
            app_tg.update_queue.put_nowait(update)
        except Exception as e:
            print(f"Ошибка при обработке апдейта: {e}")
            abort(400)
        return "ok", 200
    else:
        abort(405)


# --- main ---
if __name__ == "__main__":
    import asyncio

    async def main():
        global app_tg
        app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

        app_tg.add_handler(CommandHandler("start", start_cmd))
        app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

        # Устанавливаем вебхук
        await app_tg.bot.set_webhook(WEBHOOK_URL)

        print(f"Вебхук установлен на {WEBHOOK_URL}")

        await app_tg.start()
        await app_tg.updater.start_polling()

    asyncio.run(main())
