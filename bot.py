# bot.py
# --- Вебхук бот на Flask + httpx (без PTB/async) ---
# Переменные окружения:
#   BOT_TOKEN       — токен бота
#   WEBHOOK_SECRET  — секретный сегмент (например, mySecret_2025)
#   APP_URL         — публичный URL сервиса на Render, например:
#                     https://my-telegram-bot-xxxx.onrender.com

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass

import httpx
from flask import Flask, abort, jsonify, request


# -------------------- Конфиг --------------------

@dataclass
class Config:
    token: str
    secret: str
    app_url: str

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    @property
    def webhook_url(self) -> str:
        return f"{self.app_url.rstrip('/')}/webhook/{self.secret}"


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    secret = os.environ.get("WEBHOOK_SECRET") or "mySecret_2025"
    app_url = os.environ.get("APP_URL")

    if not token:
        print("ERROR: BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    if not app_url:
        print("ERROR: APP_URL is not set", file=sys.stderr)
        sys.exit(1)

    return Config(token=token.strip(), secret=secret.strip(), app_url=app_url.strip())


CFG = load_config()

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
CLIENT = httpx.Client(timeout=HTTP_TIMEOUT)


def tg_api(method: str, payload: dict) -> dict:
    url = f"{CFG.api_base}/{method}"
    r = CLIENT.post(url, json=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def set_webhook() -> None:
    """Удаляем старый вебхук и ставим новый."""
    try:
        tg_api("deleteWebhook", {"drop_pending_updates": False})
    except Exception as e:
        # Не критично если не было вебхука
        print(f"[init] deleteWebhook warning: {e}", file=sys.stderr)

    tg_api(
        "setWebhook",
        {
            "url": CFG.webhook_url,
            "allowed_updates": ["message", "edited_message"],
        },
    )
    print(f"[init] Webhook set -> {CFG.webhook_url}", file=sys.stderr)


# -------------------- Flask app --------------------

app = Flask(__name__)

# Ставим вебхук при импорте модуля (Flask 3 больше не поддерживает before_first_request)
try:
    set_webhook()
except Exception as e:
    # Не валим воркер — логируем и продолжаем; Telegram повторит setWebhook при следующем деплое
    print(f"[init] set_webhook error: {e}", file=sys.stderr)

# Простая статистика
STATS = {
    "start_ts": int(time.time()),
    "updates": 0,
    "last_update_ts": 0,
}


@app.get("/")
def index():
    return jsonify(
        status="ok",
        webhook_url=CFG.webhook_url,
        updates=STATS["updates"],
        up_seconds=int(time.time()) - STATS["start_ts"],
    )


@app.get("/health")
def health():
    return jsonify(status="ok")


def _handle_message(msg: dict) -> None:
    """Обработка сообщений: /start -> привет, иначе эхо."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    text = msg.get("text") or ""
    if text.startswith("/start"):
        reply = "Привет! Я на связи 🤖"
    else:
        reply = f"Ты написал: {text}" if text else "Я получил сообщение 🙂"

    tg_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": reply,
            "disable_web_page_preview": True,
        },
    )


@app.post(f"/webhook/{CFG.secret}")
def telegram_webhook():
    """Прием апдейтов от Telegram."""
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        abort(400)

    if not isinstance(payload, dict):
        abort(400)

    STATS["updates"] += 1
    STATS["last_update_ts"] = int(time.time())

    message = payload.get("message") or payload.get("edited_message")
    if message:
        try:
            _handle_message(message)
        except Exception as e:
            # Логируем, но отвечаем 200 — иначе Telegram будет ретраить
            print(
                f"[webhook] handle error: {e}\n"
                f"payload={json.dumps(payload, ensure_ascii=False)}",
                file=sys.stderr,
            )

    return jsonify(ok=True)


@app.get("/debug")
def debug():
    return jsonify(
        status="ok",
        webhook_url=CFG.webhook_url,
        stats=STATS,
    )


# Точка входа для gunicorn
app_flask = app
