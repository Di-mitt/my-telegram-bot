
# Telegram Bot on Render (webhook, sleeps on idle)

## Files
- bot.py — bot code (Flask webhook + python-telegram-bot)
- requirements.txt — dependencies
- Procfile — how to start on Render

## Render setup
1) Create New → Web Service from your GitHub repo.
2) Build Command: `pip install -r requirements.txt`
3) Start Command: `gunicorn bot:app_flask`
4) Add env vars:
   - BOT_TOKEN = your BotFather token
   - WEBHOOK_SECRET = any random string (keep private)
   - APP_URL = your Render URL (e.g., https://your-service.onrender.com)
     - If you don't know it yet, set a placeholder, deploy once, then set real URL and Redeploy.
5) After deploy, send /start to your bot. First message after sleep may have a short delay.
