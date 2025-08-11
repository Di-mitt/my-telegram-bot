# bot.py
from __future__ import annotations

import os
import time
import signal
import asyncio
import logging
import threading
from collections import deque
from typing import Optional, Deque, Tuple, Dict, Any

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

# ============ ЛОГИ ============
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ============ ENV ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("APP_URL")  # напр., https://my-telegram-bot-cr3q.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret_2025")

if not BOT_TOKEN or not APP_URL:
    raise RuntimeError("Нужны переменные окружения BOT_TOKEN и APP_URL")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{APP_URL}{WEBHOOK_PATH}"

# ============ Flask ============
app_flask = Flask(__name__)

# ============ Состояние PTB / Loop ============
_app: Optional[Application] = None
_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

_app_ready = threading.Event()       # PTB готов принимать апдейты
_app_started = threading.Event()     # PTB инициализирован и запущен
_stopping = threading.Event()        # Завершение

# ============ Буфер апдейтов ============
BUF_MAX = int(os.getenv("BUF_MAX", "200"))
BUF_TTL = int(os.getenv("BUF_TTL", "120"))  # сек
_buffer: Deque[Tuple[float, Dict[str, Any]]] = deque()
_buffer_lock = threading.Lock()

# ============ Handlers ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я на связи ✅")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"Вы написали: {update.message.text}")

# ============ Работа с буфером ============
def _enqueue_update_safe(data: Dict[str, Any]) -> None:
    """Преобразуем JSON -> Update и кладём в очередь PTB из другого потока."""
    upd = Update.de_json(data, _app.bot)  # type: ignore
    fut = asyncio.run_coroutine_threadsafe(_app.update_queue.put(upd), _loop)  # type: ignore
    # Блокируемся коротко, чтобы поймать аварии немедленно
    fut.result(timeout=2.0)

def _buffer_push(data: Dict[str, Any]) -> None:
    """Сохранить апдейт до готовности PTB (не теряем ранние апдейты)."""
    ts = time.time()
    with _buffer_lock:
        # вычищаем протухшие
        while _buffer and ts - _buffer[0][0] > BUF_TTL:
            _buffer.popleft()
        # если переполнен — вытесняем самый старый
        if len(_buffer) >= BUF_MAX:
            _buffer.popleft()
        _buffer.append((ts, data))
        log.info("Buffered update (total=%d)", len(_buffer))

def _buffer_flush_if_ready() -> int:
    """Слить буфер в PTB, если он готов. Возвращает число слитых апдейтов."""
    if not (_app and _app_ready.is_set()):
        return 0
    now = time.time()
    with _buffer_lock:
        items = [(t, d) for t, d in list(_buffer) if now - t <= BUF_TTL]
        _buffer.clear()
    flushed = 0
    for _, data in items:
        try:
            _enqueue_update_safe(data)
            flushed += 1
        except Exception:
            log.exception("Ошибка при отправке апдейта из буфера")
    if flushed:
        log.info("Flushed %d buffered updates", flushed)
    return flushed

def _buffer_flusher_daemon():
    """Фоновый флашер: подхватит ранние апдейты в моменты «дрожания» инстанса."""
    while not _stopping.is_set():
        if _app_ready.wait(timeout=0.5):
            _buffer_flush_if_ready()
        time.sleep(0.5)

# ============ PTB запуск с ретраями ============
async def _install_webhook_with_retries(app: Application) -> None:
    """Надёжная установка вебхука с повторами и экспоненциальным бэкоффом."""
    backoff = 1.0
    for attempt in range(1, 8):  # до ~127 секунд
        try:
            log.info("PTB: setting webhook to %s (attempt %d)", WEBHOOK_URL, attempt)
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            log.info("PTB: webhook is set")
            return
        except Exception:
            log.exception("PTB: failed to set webhook (attempt %d)", attempt)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    # Не удалось — но приложение всё равно живёт, апдейты будут приходить позже
    log.error("PTB: webhook was not set after retries")

async def _ptb_main():
    global _app
    log.info("PTB: building application...")
    _app = ApplicationBuilder().token(BOT_TOKEN).build()
    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    await _app.initialize()
    await _app.start()
    _app_started.set()  # PTB жив и имеет очередь апдейтов

    # Ставим вебхук c ретраями
    await _install_webhook_with_retries(_app)

    # Считаем PTB полностью готовым — можно флашить буфер
    _app_ready.set()
    _buffer_flush_if_ready()

    # Держим задачу живой
    await asyncio.Event().wait()

def _ptb_thread_runner():
    """Запускаем отдельный event loop, чтобы Flask/WSGI не мешал PTB."""
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ptb_main())
    except Exception:
        log.exception("PTB thread crashed")
    finally:
        if _app:
            try:
                _loop.run_until_complete(_app.stop())
            except Exception:
                pass
        _loop.close()

# Стартуем при импорте — это важно под gunicorn
threading.Thread(target=_ptb_thread_runner, daemon=True, name="ptb-runner").start()
threading.Thread(target=_buffer_flusher_daemon, daemon=True, name="buffer-flusher").start()

# ============ Завершение ============
def _on_term(signum, frame):
    log.info("Получен сигнал %s, останавливаемся...", signum)
    _stopping.set()
    # даём флашеру дойти цикл
    try:
        _buffer_flush_if_ready()
    except Exception:
        pass

signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)

# ============ HTTP маршруты ============
@app_flask.get("/")
def health():
    # Быстрый ответ для Render/healthcheck
    return "OK", 200

@app_flask.post(WEBHOOK_PATH)
def webhook_receiver():
    # Проверяем секретный заголовок Telegram
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return abort(403)

    if _stopping.is_set():
        # На остановке — просто принимаем, чтобы Telegram не ретраил бесконечно
        return "ok", 200

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        log.exception("Bad JSON in webhook")
        return "ok", 200
    if not data:
        return "ok", 200

    # Если PTB уже готов — кладём апдейт напрямую, иначе — буферизуем
    if _app_ready.is_set():
        try:
            _enqueue_update_safe(data)
        except Exception:
            log.exception("enqueue failed")
        return "ok", 200
    else:
        _buffer_push(data)
        return "ok", 200

# Опционально: вручную посмотреть, сколько в буфере
@app_flask.get("/buffer")
def buffer_stats():
    with _buffer_lock:
        size = len(_buffer)
    return {"buffered": size, "ready": _app_ready.is_set(), "started": _app_started.is_set()}, 200

# ============ Локальный запуск (не используется на Render с gunicorn) ============
if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
