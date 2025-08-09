import logging
import os
import asyncio
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")
PORT = int(os.getenv("PORT", 10000))
BASE_URL = os.getenv("BASE_URL")  # пример: https://my-telegram-bot.onrender.com

ptb_ready = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я на связи 🤖")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(update.message.text)

async def main():
    global ptb_ready
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Запуск PTB в фоне
    asyncio.create_task(application.initialize())
    asyncio.create_task(application.start())
    ptb_ready = True

    # Даем время PTB полностью подняться
    logger.info("Ожидаем 3 секунды перед установкой вебхука...")
    await asyncio.sleep(3)

    # Устанавливаем вебхук
    webhook_url = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
    await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
    logger.info(f"Webhook установлен: {webhook_url}")

    # Создаем aiohttp-сервер
    async def handle(request):
        if request.match_info.get("token") != WEBHOOK_SECRET:
            return web.Response(status=403)
        data = await request.json()
        if ptb_ready:
            await application.update_queue.put(Update.de_json(data, application.bot))
        else:
            logger.warning("Получено обновление, но PTB еще не готов")
        return web.Response()

    app = web.Application()
    app.router.add_post("/webhook/{token}", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Сервер запущен на порту {PORT}")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
    
