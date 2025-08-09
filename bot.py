# bot.py
import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
APP_URL = os.environ["APP_URL"]          # например, https://my-telegram-bot.onrender.com
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

async def on_startup(app: Application):
    # Сбрасываем старый вебхук и ставим новый с секретом
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        url_path=WEBHOOK_PATH,         # <-- путь с секретом
        webhook_url=WEBHOOK_URL,       # <-- полный URL вебхука
        secret_token=WEBHOOK_SECRET,   # <-- PTB сам проверит заголовок
        on_startup=[on_startup],
    )
