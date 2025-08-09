# bot.py
import os
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters,
)

# ======= ENV =======
BOT_TOKEN = os.environ["BOT_TOKEN"]                # обязателен
APP_URL = os.environ["APP_URL"]                   # напр. https://my-telegram-bot.onrender.com
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ======= Flask app (важно: на верхнем уровне!) =======
app_flask = Flask(__name__)

# ======= Telegram application =======
app_tg: Application = ApplicationBuilder().token(BOT_TOKEN).build()

# handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

app_tg.add_handler(CommandHandler("start", start_cmd))
app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

# startup: включаем приложение и устанавливаем вебхук
async def _startup(app: Application):
    # инициализация/старт PTB без собственного веб-сервера
    await app.initialize()
    await app.start()
    # сбрасываем и ставим вебхук на адрес нашего Flask
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)

# запускаем асинхронный старт PTB в фоне при импорте модуля
import asyncio
asyncio.get_event_loop().create_task(_startup(app_tg))

# ======= Flask route, куда Telegram шлет апдейты =======
@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    # проверка секретного хедера
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    json_update = request.get_json(force=True)
    # прокидываем апдейт в очередь PTB
    app_tg.update_queue.put_nowait(Update.de_json(json_update, app_tg.bot))
    return "ok"
