
import os
from flask import Flask, request, abort
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.ext._application import Application  # for type hints

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL   = os.getenv("APP_URL")   # e.g. https://your-service.onrender.com
SECRET    = os.getenv("WEBHOOK_SECRET", "change-me-secret")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Set env vars BOT_TOKEN and APP_URL")

WEBHOOK_PATH = f"/webhook/{SECRET}"
WEBHOOK_URL  = f"{APP_URL}{WEBHOOK_PATH}"

app_flask = Flask(__name__)
app_tg: Application | None = None

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я проснулся и на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

async def on_startup(application: Application):
    # Reset webhook then set ours
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=WEBHOOK_URL)
    print("Webhook set to:", WEBHOOK_URL)

def build_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    return application

@app_flask.route("/", methods=["GET"])
def health():
    return "OK", 200

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = Update.de_json(request.get_json(force=True), app_tg.bot)
    app_tg.update_queue.put_nowait(update)
    return "OK", 200

if __name__ == "__main__":
    # Local dev: run polling (no webhook)
    print("Running locally with polling...")
    local_app = build_app()
    local_app.run_polling()
else:
    # Render deploy: create PTB application; Flask is served by gunicorn
    app_tg = build_app()
