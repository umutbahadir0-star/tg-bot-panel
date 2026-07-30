import asyncio
import sqlite3
from datetime import datetime
from telegram.ext import Application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn
import os

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8000))
DB = "bot.db"

scheduler = AsyncIOScheduler()
application = None

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY,
                    message TEXT DEFAULT 'Merhaba, bu otomatik mesajdır.',
                    interval_seconds INTEGER DEFAULT 3600
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    added_at TEXT
                )""")
    c.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
    conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT message, interval_seconds FROM settings WHERE id=1").fetchone()
    conn.close()
    return row

def update_message(text):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE settings SET message=? WHERE id=1", (text,))
    conn.commit()
    conn.close()

def update_interval(seconds):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE settings SET interval_seconds=? WHERE id=1", (seconds,))
    conn.commit()
    conn.close()

def add_chat(chat_id, title=""):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR REPLACE INTO chats (chat_id, title, added_at) VALUES (?, ?, ?)",
                 (chat_id, title, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def remove_chat(chat_id):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def get_chats():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT chat_id, title, added_at FROM chats ORDER BY added_at DESC").fetchall()
    conn.close()
    return rows

async def send_to_all():
    msg, _ = get_settings()
    chats = get_chats()
    bot = application.bot
    for chat_id, title, _ in chats:
        try:
            await bot.send_message(chat_id=chat_id, text=msg)
            print(f"Gonderildi → {chat_id}")
        except Exception as e:
            print(f"Hata {chat_id}: {e}")

def restart_scheduler():
    scheduler.remove_all_jobs()
    _, interval = get_settings()
    scheduler.add_job(send_to_all, "interval", seconds=interval, id="main_job")
    if not scheduler.running:
        scheduler.start()
    print(f"Zamanlayici → her {interval} saniyede bir")

app = FastAPI()

HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Yönetim Paneli</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, sans-serif; background: #0f0f0f; color: #e0e0e0; padding: 20px; }}
        .container {{ max-width: 640px; margin: 0 auto; }}
        h1 {{ color: #fff; margin-bottom: 6px; }}
        .sub {{ color: #777; margin-bottom: 24px; font-size: 0.9rem; }}
        .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px; padding: 18px; margin-bottom: 16px; }}
        .card h2 {{ font-size: 1rem; margin-bottom: 12px; color: #fff; }}
        textarea, input {{ width: 100%; padding: 11px; border: 1px solid #333; border-radius: 8px; background: #111; color: #fff; margin-bottom: 10px; font-size: 0.95rem; }}
        textarea {{ min-height: 90px; resize: vertical; }}
        button {{ background: #2aabee; color: #fff; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
        button.danger {{ background: #e74c3c; }}
        .status {{ background: #143d2a; color: #2ecc71; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 18px; display: inline-block; }}
        .chat-item {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #2a2a2a; }}
        .chat-id {{ font-family: monospace; color: #2aabee; }}
        .empty {{ color: #555; font-size: 0.9rem; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Bot Yönetim Paneli</h1>
    <p class="sub">Mesaj, aralık ve grupları buradan yönet</p>
    <div class="status">● Sistem çalışıyor · Aralık: {interval} sn</div>
    <div class="card">
        <h2>Gönderilecek Mesaj</h2>
        <form method="post" action="/set_message">
            <textarea name="message" required>{message}</textarea>
            <button type="submit">Mesajı Kaydet</button>
        </form>
    </div>
    <div class="card">
        <h2>Gönderim Aralığı (saniye)</h2>
        <form method="post" action="/set_interval">
            <input type="number" name="seconds" value="{interval}" min="1" required>
            <button type="submit">Aralığı Güncelle</button>
        </form>
    </div>
    <div class="card">
        <h2>Grup / Kanal Ekle</h2>
        <form method="post" action="/add_chat">
            <input type="text" name="chat_id" placeholder="-1001234567890" required>
            <button type="submit">Grubu Ekle</button>
        </form>
        <p style="margin-top:8px;font-size:0.8rem;color:#666;">Chat ID için gruba @RawDataBot ekle</p>
    </div>
    <div class="card">
        <h2>Kayıtlı Gruplar ({chat_count})</h2>
        {chat_list}
    </div>
</div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    message, interval = get_settings()
    chats = get_chats()
    if chats:
        chat_html = ""
        for cid, title, added in chats:
            chat_html += f"""
            <div class="chat-item">
                <div>
                    <div class="chat-id">{cid}</div>
                    <div style="font-size:0.8rem;color:#666">{title or 'İsimsiz'} · {added[:16]}</div>
                </div>
                <form method="post" action="/remove_chat" style="margin:0">
                    <input type="hidden" name="chat_id" value="{cid}">
                    <button type="submit" class="danger" style="padding:5px 10px;font-size:0.8rem">Sil</button>
                </form>
            </div>"""
    else:
        chat_html = '<p class="empty">Henüz grup yok</p>'
    return HTML.format(message=message, interval=interval, chat_count=len(chats), chat_list=chat_html)

@app.post("/set_message")
async def set_message(message: str = Form(...)):
    update_message(message.strip())
    return RedirectResponse("/", status_code=303)

@app.post("/set_interval")
async def set_interval(seconds: int = Form(...)):
    update_interval(max(1, seconds))
    restart_scheduler()
    return RedirectResponse("/", status_code=303)

@app.post("/add_chat")
async def add_chat_web(chat_id: str = Form(...)):
    try:
        add_chat(int(chat_id.strip()))
    except:
        pass
    return RedirectResponse("/", status_code=303)

@app.post("/remove_chat")
async def remove_chat_web(chat_id: int = Form(...)):
    remove_chat(chat_id)
    return RedirectResponse("/", status_code=303)

async def main():
    global application
    if not TOKEN:
        print("HATA: BOT_TOKEN yok!")
        return
    init_db()
    application = Application.builder().token(TOKEN).build()
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    restart_scheduler()
    print(f"Site hazır → port {PORT}")
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
