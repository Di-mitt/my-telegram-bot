import os
import json
import logging
import threading
import asyncio
from collections import deque
from typing import Deque, Tuple, Optional

from flask import Flask, request, abort, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


# -------------------- ENV ---------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # https://<service>.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is not set")
if not APP_URL:
    raise RuntimeError("ENV APP_URL is not set")
if not WEBHOOK_SECRET:
    raise RuntimeError("ENV WEBHOOK_SECRET is not set")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"


# -------------------- FLASK -------------------
app_flask = Flask(__name__)

# PTB runtime объекты
_application: Optional[Application] = None
_ptb_loop: Optional[asyncio.AbstractEventLoop] = None
_ready = threading.Event()              # станет True, когда PTB полностью готов
_buffer: Deque[Tuple[dict, dict]] = deque()  # (json, headers) — апдейты, пришедшие до готовности
_buffer_lock = threading.Lock()


# -------------------- HANDLERS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я на связи 🤖")


# -------------------- PTB SETUP ----------------
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    return app


async def _ptb_async_runner():
    """
    Запускается внутри отдельного event loop.
    Без блокировок запускает PTB и ставит вебхук.
    """
    global _application

    log.info("PTB: building application...")
    _application = build_application()

    # Старт без блокировки
    await _application.initialize()
    await _application.start()

    # Ставим вебхук
    try:
        log.info("PTB: setting webhook to %s", WEBHOOK_URL)
        await _application.bot.delete_webhook(drop_pending_updates=False)
        await _application.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=None,   # все типы
        )
        log.info("PTB: webhook is set")
    except Exception:  # noqa: BLE001
        log.exception("PTB: failed to set webhook")
        raise

    # Отмечаем готовность, затем догоним буфер
    _ready.set()
    _drain_buffer()

    # Держим цикл живым
    while True:
        await asyncio.sleep(3600)


def _start_ptb_thread():
    """
    Создаёт отдельный поток и event loop для PTB, чтобы
    из Flask можно было безопасно отправлять задачи в loop.
    """
    global _ptb_loop

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # запомним loop, чтобы подавать в него задачи из Flask
        global _ptb_loop
        _ptb_loop = loop
        try:
            loop.run_until_complete(_ptb_async_runner())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    th = threading.Thread(target=_runner, daemon=True, name="ptb-runner")
    th.start()


def _submit_to_ptb(coro: asyncio.Future):
    """
    Безопасно отправить корутину в loop PTB из Flask-потока.
    """
    if not _ptb_loop:
        raise RuntimeError("PTB loop is not ready")
    asyncio.run_coroutine_threadsafe(coro, _ptb_loop)


def _drain_buffer():
    """
    После готовности PTB разгрести накопленные запросы.
    """
    if not _ready.is_set() or not _application:
        return
    drained = 0
    with _buffer_lock:
        while _buffer:
            data, headers = _buffer.popleft()
            try:
                update = Update.de_json(data, _application.bot)
                _submit_to_ptb(_application.process_update(update))
                drained += 1
            except Exception:  # noqa: BLE001
                log.exception("Failed to process buffered update")
    if drained:
        log.info("Buffered queue drained: %s updates", drained)


# -------------------- FLASK ROUTES -------------
@app_flask.get("/")
def health():
    return "OK", 200


@app_flask.post(WEBHOOK_PATH)
def webhook():
    """
    Синхронный Flask-роут:
      1) проверяем секретный заголовок
      2) если PTB не готов — буферизуем апдейт и возвращаем 200 (чтобы Telegram не долбил ретраями)
      3) если готов — передаём апдейт в event loop PTB
    """
    # Проверка секрета
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secret or secret != WEBHOOK_SECRET:
        abort(403)

    # Получаем JSON
    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            raise ValueError("Payload is not a dict")
    except Exception as e:  # noqa: BLE001
        log.warning("Bad webhook JSON: %s", e)
        return Response("bad request", status=400)

    # Если PTB ещё не готов — складываем в буфер
    if not _ready.is_set() or _application is None:
        with _buffer_lock:
            _buffer.append((data, dict(request.headers)))
        # 200, чтобы Telegram не ретраил, мы сами догоним буфер
        return Response("buffered", status=200)

    # Готов — шлём в PTB
    try:
        update = Update.de_json(data, _application.bot)
        _submit_to_ptb(_application.process_update(update))
    except Exception:  # noqa: BLE001
        log.exception("Failed to submit update to PTB")
        return Response("fail", status=500)

    return Response("ok", status=200)


# -------------------- ENTRYPOINT ----------------
# Запускаем PTB в отдельном потоке сразу при импорте модуля,
# чтобы к моменту прихода первых вебхуков он успел подняться.
_start_ptb_thread()

# Ничего больше делать не надо — gunicorn поднимет Flask-приложение (app_flask)
# и Render увидит порт, потому что gunicorn слушает $PORT.
# Команда запуска в Render:  gunicorn bot:app_flask
