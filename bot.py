# bot.py
import os
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # Например: https://my-telegram-bot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

app_flask = Flask(__name__)
app_tg: Application | None = None  # Создастся ниже


# === handlers ===
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# === Telegram startup ===
async def on_startup(application: Application):
    # Сброс вебхука, затем установка нового
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=WEBHOOK_URL)


# === Flask route ===
@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    if "X-Telegram-Bot-Api-Secret-Token" not in request.headers or \
       request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    json_update = request.get_json(force=True)
    if app_tg:
        app_tg.update_queue.put_nowait(Update.de_json(json_update, app_tg.bot))
    return "ok"


if __name__ == "__main__":
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    app_tg.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET
    )
