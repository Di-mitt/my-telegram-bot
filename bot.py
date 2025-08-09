from __future__ import annotations
import os
import logging
from flask import Flask, request, abort
from telegram import Update, Bot
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========== Логи ==========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ========== Переменные окружения ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")  # !!! на Render в Env Vars
APP_URL = os.getenv("APP_URL")      # например: https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set BOT_TOKEN and APP_URL in environment variables")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ========== Flask ==========
app_flask = Flask(__name__)
app_tg: Application | None = None


# ======= Handlers =======
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")


# ======= Webhook setter =======
async def set_webhook(bot: Bot):
    log.info(f"Setting webhook to {WEBHOOK_URL}")
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook is set")


# ======= Flask routes =======
@app_flask.route("/", methods=["GET"])
def health():
    return "OK", 200


@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        abort(403)

    global app_tg
    if not app_tg:
        log.error("Webhook got request, but bot is not ready yet")
        return "ok", 200

    data = request.get_json(force=True)
    update = Update.de_json(data, app_tg.bot)
    app_tg.update_queue.put_nowait(update)
    return "ok", 200


# ======= Запуск =======
if __name__ == "__main__":
    log.info("Starting bot application...")
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    # handlers
    app_tg.add_handler(CommandHandler("start", start_cmd))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Устанавливаем вебхук перед стартом
    import asyncio
    asyncio.run(set_webhook(app_tg.bot))

    # Запускаем бота в отдельном потоке
    import threading
    threading.Thread(target=app_tg.run_polling, daemon=True).start()

    # Flask (Render запускает gunicorn bot:app_flask)
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
