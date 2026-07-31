import asyncio
import re
import sqlite3
from datetime import datetime, timedelta
from telegram import ChatPermissions
from telegram.ext import Application, MessageHandler, CommandHandler, filters as tg_filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import uvicorn
import os

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8000))
DB = "bot.db"
PHOTO_PATH = "uploaded_photo.jpg"

# --- Flood ayarları ---
FLOOD_LIMIT = 5          # bu kadar mesaj
FLOOD_WINDOW = 8         # bu kadar saniye içinde atılırsa
FLOOD_MUTE_MINUTES = 5   # bu kadar dakika susturulur

scheduler = AsyncIOScheduler()
application = None
flood_tracker = {}  # {(chat_id, user_id): [timestamp, timestamp, ...]}

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY,
                    message TEXT DEFAULT 'Merhaba, bu otomatik mesajdır.',
                    interval_seconds INTEGER DEFAULT 3600,
                    photo_path TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    added_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    ts TEXT
                )""")
    c.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
    try:
        c.execute("ALTER TABLE settings ADD COLUMN photo_path TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT message, interval_seconds, photo_path FROM settings WHERE id=1").fetchone()
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

def update_photo(path):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE settings SET photo_path=? WHERE id=1", (path,))
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

def log_message(chat_id, user_id, username):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO message_log (chat_id, user_id, username, ts) VALUES (?,?,?,?)",
                 (chat_id, user_id, username, datetime.now().isoformat()))
    conn.commit()
    conn.close()

async def send_to_all():
    msg, _, photo_path = get_settings()
    chats = get_chats()
    bot = application.bot
    for chat_id, title, _ in chats:
        try:
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, "rb") as f:
                    await bot.send_photo(chat_id=chat_id, photo=f, caption=msg)
            else:
                await bot.send_message(chat_id=chat_id, text=msg)
            print(f"Gonderildi → {chat_id}")
        except Exception as e:
            print(f"Hata {chat_id}: {e}")

def restart_scheduler():
    scheduler.remove_all_jobs()
    _, interval, _ = get_settings()
    scheduler.add_job(send_to_all, "interval", seconds=interval, id="main_job")
    if not scheduler.running:
        scheduler.start()
    print(f"Zamanlayici → her {interval} saniyede bir")

# ---------------- Süre parse etme (5dakika, 2saat, 1gun vb.) ----------------
UNIT_MAP = {
    "saniye": "seconds", "sn": "seconds",
    "dakika": "minutes", "dk": "minutes",
    "saat": "hours", "sa": "hours",
    "gun": "days", "gün": "days",
}

def parse_duration(text):
    m = re.match(r"^(\d+)\s*(saniye|sn|dakika|dk|saat|sa|gün|gun)$", text.strip().lower())
    if not m:
        return None
    val = int(m.group(1))
    unit = UNIT_MAP[m.group(2)]
    return timedelta(**{unit: val})

def format_duration(td: timedelta):
    total = int(td.total_seconds())
    if total % 86400 == 0:
        return f"{total // 86400} gün"
    if total % 3600 == 0:
        return f"{total // 3600} saat"
    if total % 60 == 0:
        return f"{total // 60} dakika"
    return f"{total} saniye"

async def is_admin(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ---------------- Flood kontrolü ----------------
async def check_flood(update, context):
    msg = update.message
    chat_id = msg.chat_id
    user = msg.from_user
    key = (chat_id, user.id)
    now = datetime.now().timestamp()
    times = [t for t in flood_tracker.get(key, []) if now - t <= FLOOD_WINDOW]
    times.append(now)
    flood_tracker[key] = times
    if len(times) < FLOOD_LIMIT:
        return
    flood_tracker[key] = []
    if await is_admin(context, chat_id, user.id):
        return
    until = datetime.now() + timedelta(minutes=FLOOD_MUTE_MINUTES)
    try:
        await context.bot.restrict_chat_member(
            chat_id, user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await context.bot.send_message(
            chat_id,
            f"🚨 {user.first_name} flood yaptığı için {FLOOD_MUTE_MINUTES} dakika susturuldu."
        )
    except Exception as e:
        print(f"Flood mute hatası: {e}")

# ---------------- Genel mesaj handler'ı (loglama + spam foto/sticker silme + flood) ----------------
async def universal_handler(update, context):
    msg = update.message
    if not msg or not msg.from_user:
        return
    if msg.from_user.id == context.bot.id:
        return  # botun kendi mesajlarını sayma/silme

    log_message(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name)

    if msg.chat.type in ("group", "supergroup"):
        await check_flood(update, context)

    if msg.photo or msg.sticker:
        try:
            await msg.delete()
            print(f"Silindi → chat {msg.chat_id}, kullanıcı {msg.from_user.id}")
        except Exception as e:
            print(f"Silme hatası: {e}")

# ---------------- /mute ----------------
async def cmd_mute(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if msg.chat.type not in ("group", "supergroup"):
        await msg.reply_text("Bu komut sadece gruplarda çalışır.")
        return
    if not await is_admin(context, chat_id, msg.from_user.id):
        await msg.reply_text("Bu komutu sadece adminler kullanabilir.")
        return
    if not msg.reply_to_message:
        await msg.reply_text("Kullanım: susturmak istediğin kişinin mesajına reply atıp '/mute 5dakika' yaz.")
        return
    if not context.args:
        await msg.reply_text("Süre belirt. Örnek: /mute 5dakika, /mute 2saat, /mute 1gun")
        return
    duration = parse_duration(context.args[0])
    if duration is None:
        await msg.reply_text("Süre formatı hatalı. Örnek: 5dakika, 10dk, 2saat, 1gun, 30saniye")
        return
    target = msg.reply_to_message.from_user
    until = datetime.now() + duration
    try:
        await context.bot.restrict_chat_member(
            chat_id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await msg.reply_text(f"🔇 {target.first_name} {format_duration(duration)} boyunca susturuldu.")
    except Exception as e:
        await msg.reply_text(f"Susturma başarısız oldu: {e}")

# ---------------- /unmute ----------------
async def cmd_unmute(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if msg.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(context, chat_id, msg.from_user.id):
        await msg.reply_text("Bu komutu sadece adminler kullanabilir.")
        return
    if not msg.reply_to_message:
        await msg.reply_text("Susturmayı kaldırmak istediğin kişinin mesajına reply atıp '/unmute' yaz.")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            chat_id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        await msg.reply_text(f"🔊 {target.first_name} susturması kaldırıldı.")
    except Exception as e:
        await msg.reply_text(f"İşlem başarısız oldu: {e}")

# ---------------- /gunluk ve /aylik ----------------
def stats_text(chat_id, prefix, title):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """SELECT username, user_id, COUNT(*) c FROM message_log
           WHERE chat_id=? AND ts LIKE ? GROUP BY user_id ORDER BY c DESC LIMIT 25""",
        (chat_id, f"{prefix}%")
    ).fetchall()
    conn.close()
    if not rows:
        return f"{title}\n\nHenüz mesaj yok."
    lines = [title, ""]
    for i, (username, uid, count) in enumerate(rows, 1):
        name = username or f"id:{uid}"
        lines.append(f"{i}. {name} — {count} mesaj")
    total = sum(r[2] for r in rows)
    lines.append("")
    lines.append(f"Toplam: {total} mesaj · {len(rows)} kişi")
    return "\n".join(lines)

async def cmd_gunluk(update, context):
    chat_id = update.message.chat_id
    today = datetime.now().date().isoformat()
    await update.message.reply_text(stats_text(chat_id, today, "📊 Bugünkü Mesaj İstatistikleri"))

async def cmd_aylik(update, context):
    chat_id = update.message.chat_id
    month = datetime.now().strftime("%Y-%m")
    await update.message.reply_text(stats_text(chat_id, month, "📈 Bu Ayki Mesaj İstatistikleri"))

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
        .status2 {{ background: #3d2a14; color: #f39c12; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 18px; margin-left: 8px; display: inline-block; }}
        .chat-item {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #2a2a2a; }}
        .chat-id {{ font-family: monospace; color: #2aabee; }}
        .empty {{ color: #555; font-size: 0.9rem; }}
        .photo-preview {{ width: 100%; max-width: 260px; border-radius: 8px; margin-bottom: 10px; display: block; }}
        .file-input {{ background: #111; border: 1px dashed #333; padding: 10px; }}
        code {{ background: #111; padding: 2px 6px; border-radius: 4px; color: #2aabee; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Bot Yönetim Paneli</h1>
    <p class="sub">Mesaj, aralık ve grupları buradan yönet</p>
    <div class="status">● Sistem çalışıyor · Aralık: {interval} sn</div>
    <div class="status2">🛡 Flood + spam koruması aktif</div>
    <div class="card">
        <h2>Gönderilecek Mesaj</h2>
        <form method="post" action="/set_message">
            <textarea name="message" required>{message}</textarea>
            <button type="submit">Mesajı Kaydet</button>
        </form>
    </div>
    <div class="card">
        <h2>Fotoğraf (opsiyonel)</h2>
        {photo_preview}
        <form method="post" action="/set_photo" enctype="multipart/form-data">
            <input class="file-input" type="file" name="photo" accept="image/*" required>
            <button type="submit">Fotoğraf Yükle</button>
        </form>
        {remove_photo_form}
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
        <h2>Grup Komutları</h2>
        <p style="font-size:0.85rem;color:#999;line-height:1.8">
            <code>/mute 5dakika</code> — reply atılan kişiyi susturur (admin)<br>
            <code>/unmute</code> — reply atılan kişinin susturmasını kaldırır (admin)<br>
            <code>/gunluk</code> — bugünkü mesaj istatistikleri<br>
            <code>/aylik</code> — bu ayki mesaj istatistikleri<br>
            Flood: {flood_limit} mesaj / {flood_window} sn → otomatik {flood_mute} dk mute
        </p>
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
    message, interval, photo_path = get_settings()
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

    if photo_path and os.path.exists(photo_path):
        photo_preview = '<img class="photo-preview" src="/photo">'
        remove_photo_form = """
        <form method="post" action="/remove_photo" style="margin-top:8px">
            <button type="submit" class="danger">Fotoğrafı Kaldır</button>
        </form>"""
    else:
        photo_preview = '<p class="empty" style="margin-bottom:10px">Fotoğraf yok · sadece metin gönderilecek</p>'
        remove_photo_form = ""

    return HTML.format(message=message, interval=interval, chat_count=len(chats),
                        chat_list=chat_html, photo_preview=photo_preview,
                        remove_photo_form=remove_photo_form,
                        flood_limit=FLOOD_LIMIT, flood_window=FLOOD_WINDOW,
                        flood_mute=FLOOD_MUTE_MINUTES)

@app.get("/photo")
async def get_photo():
    if os.path.exists(PHOTO_PATH):
        return FileResponse(PHOTO_PATH)
    return RedirectResponse("/")

@app.post("/set_message")
async def set_message(message: str = Form(...)):
    update_message(message.strip())
    return RedirectResponse("/", status_code=303)

@app.post("/set_photo")
async def set_photo(photo: UploadFile = File(...)):
    content = await photo.read()
    with open(PHOTO_PATH, "wb") as f:
        f.write(content)
    update_photo(PHOTO_PATH)
    return RedirectResponse("/", status_code=303)

@app.post("/remove_photo")
async def remove_photo():
    update_photo(None)
    if os.path.exists(PHOTO_PATH):
        os.remove(PHOTO_PATH)
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

    application.add_handler(CommandHandler("mute", cmd_mute))
    application.add_handler(CommandHandler("unmute", cmd_unmute))
    application.add_handler(CommandHandler("gunluk", cmd_gunluk))
    application.add_handler(CommandHandler("aylik", cmd_aylik))
    application.add_handler(MessageHandler(tg_filters.ALL & ~tg_filters.COMMAND, universal_handler))

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
