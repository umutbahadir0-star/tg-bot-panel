import asyncio
import random
import re
import sqlite3
from datetime import datetime, timedelta
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters as tg_filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import uvicorn
import os

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8000))
DB = "bot.db"
PHOTO_PATH = "uploaded_photo.jpg"

scheduler = AsyncIOScheduler()
application = None

flood_tracker = {}       # {(chat_id, user_id): [timestamp, ...]}
duplicate_tracker = {}   # {(chat_id, user_id): (last_text, tekrar_sayisi)}
blackjack_games = {}     # {(chat_id, user_id): {...}}

LINK_RE = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)
PHONE_RE = re.compile(r"[\d][\d\s\-\(\)]{7,}\d")
UNIT_SECONDS = {"ms": 0.001, "sn": 1, "dk": 60}


def to_seconds(value, unit):
    return float(value) * UNIT_SECONDS.get(unit, 1)


def contains_phone_number(text):
    for group in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", group)
        if 10 <= len(digits) <= 13:
            return True
    return False


# ==================== DB ====================
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
    c.execute("""CREATE TABLE IF NOT EXISTS flood_warnings (
                    chat_id INTEGER,
                    user_id INTEGER,
                    warn_count INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS xox_games (
                    chat_id INTEGER PRIMARY KEY,
                    player_x INTEGER,
                    player_x_name TEXT,
                    player_o INTEGER,
                    player_o_name TEXT,
                    board TEXT DEFAULT '_________',
                    turn TEXT DEFAULT 'X',
                    vs_ai INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active'
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS banned_words (
                    word TEXT PRIMARY KEY
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS xp (
                    chat_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    xp INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS guess_games (
                    chat_id INTEGER PRIMARY KEY,
                    target INTEGER,
                    attempts INTEGER DEFAULT 0
                )""")
    c.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")

    for stmt in [
        "ALTER TABLE settings ADD COLUMN photo_path TEXT",
        "ALTER TABLE settings ADD COLUMN interval_value REAL DEFAULT 3600",
        "ALTER TABLE settings ADD COLUMN interval_unit TEXT DEFAULT 'sn'",
        "ALTER TABLE settings ADD COLUMN flood_limit INTEGER DEFAULT 5",
        "ALTER TABLE settings ADD COLUMN flood_window INTEGER DEFAULT 8",
        "ALTER TABLE settings ADD COLUMN flood_mute_minutes INTEGER DEFAULT 5",
        "ALTER TABLE settings ADD COLUMN link_block INTEGER DEFAULT 0",
        "ALTER TABLE chats ADD COLUMN active INTEGER DEFAULT 1",
    ]:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass

    row = c.execute("SELECT interval_seconds, interval_value FROM settings WHERE id=1").fetchone()
    if row and row[1] in (None, 3600) and row[0]:
        c.execute("UPDATE settings SET interval_value=?, interval_unit='sn' WHERE id=1", (row[0],))

    conn.commit()
    conn.close()


def get_settings():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
    conn.close()
    return dict(row)


def update_message(text):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE settings SET message=? WHERE id=1", (text,))
    conn.commit()
    conn.close()


def update_interval(value, unit):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE settings SET interval_value=?, interval_unit=? WHERE id=1", (value, unit))
    conn.commit()
    conn.close()


def update_photo(path):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE settings SET photo_path=? WHERE id=1", (path,))
    conn.commit()
    conn.close()


def update_flood(limit, window, mute, link_block):
    conn = sqlite3.connect(DB)
    conn.execute(
        "UPDATE settings SET flood_limit=?, flood_window=?, flood_mute_minutes=?, link_block=? WHERE id=1",
        (limit, window, mute, link_block),
    )
    conn.commit()
    conn.close()


def add_chat(chat_id, title=""):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR REPLACE INTO chats (chat_id, title, added_at, active) "
        "VALUES (?, ?, ?, COALESCE((SELECT active FROM chats WHERE chat_id=?), 1))",
        (chat_id, title, datetime.now().isoformat(), chat_id),
    )
    conn.commit()
    conn.close()


def remove_chat(chat_id):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def toggle_chat(chat_id):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE chats SET active = 1 - COALESCE(active,1) WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def get_chats():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT chat_id, title, added_at, COALESCE(active,1) as active FROM chats ORDER BY added_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_message(chat_id, user_id, username):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO message_log (chat_id, user_id, username, ts) VALUES (?,?,?,?)",
                 (chat_id, user_id, username, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_warn_count(chat_id, user_id):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT warn_count FROM flood_warnings WHERE chat_id=? AND user_id=?",
                        (chat_id, user_id)).fetchone()
    conn.close()
    return row[0] if row else 0


def bump_warn_count(chat_id, user_id):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO flood_warnings (chat_id, user_id, warn_count) VALUES (?,?,1) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET warn_count = warn_count + 1",
        (chat_id, user_id),
    )
    conn.commit()
    conn.close()


def get_banned_words():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT word FROM banned_words").fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_banned_words(words):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM banned_words")
    seen = set()
    for w in words:
        w = w.strip().lower()
        if w and w not in seen:
            seen.add(w)
            conn.execute("INSERT OR IGNORE INTO banned_words (word) VALUES (?)", (w,))
    conn.commit()
    conn.close()


def contains_banned_word(text, words):
    lowered = text.lower()
    return any(w in lowered for w in words if w)


def bump_xp(chat_id, user_id, username, amount=1):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO xp (chat_id, user_id, username, xp) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET xp = xp + excluded.xp, username = excluded.username",
        (chat_id, user_id, username, amount),
    )
    conn.commit()
    conn.close()


def set_xp_absolute(chat_id, user_id, username, amount):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO xp (chat_id, user_id, username, xp) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET xp = excluded.xp, username = excluded.username",
        (chat_id, user_id, username, amount),
    )
    conn.commit()
    conn.close()


def get_xp(chat_id, user_id):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT xp FROM xp WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    conn.close()
    return row[0] if row else 0


def get_leaderboard(chat_id, limit=10):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT username, xp FROM xp WHERE chat_id=? ORDER BY xp DESC LIMIT ?", (chat_id, limit)
    ).fetchall()
    conn.close()
    return rows


def level_from_xp(xp):
    return xp // 100 + 1


def xp_to_next(xp):
    lvl = level_from_xp(xp)
    return lvl * 100 - xp


def guess_start(chat_id, target):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT OR REPLACE INTO guess_games (chat_id, target, attempts) VALUES (?,?,0)", (chat_id, target))
    conn.commit()
    conn.close()


def guess_get(chat_id):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM guess_games WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def guess_bump(chat_id):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE guess_games SET attempts = attempts + 1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def guess_end(chat_id):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM guess_games WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


# ==================== Yayın (broadcast) ====================
async def send_to_all():
    s = get_settings()
    msg, photo_path = s["message"], s["photo_path"]
    chats = [c for c in get_chats() if c["active"]]
    bot = application.bot
    for c in chats:
        chat_id = c["chat_id"]
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
    for job in scheduler.get_jobs():
        if job.id == "main_job":
            scheduler.remove_job("main_job")
    s = get_settings()
    seconds = max(0.05, to_seconds(s["interval_value"], s["interval_unit"]))
    scheduler.add_job(send_to_all, "interval", seconds=seconds, id="main_job")
    if not scheduler.running:
        scheduler.start()
    print(f"Zamanlayici → her {seconds} saniyede bir")


# ==================== Süre parse etme (/mute için) ====================
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


# ==================== Ortak ceza sistemi ====================
async def force_restrict(context, chat_id, user, until):
    """Kullanıcıyı yetkili (administrator) bile olsa susturmaya zorlar.
    Telegram, bot ne kadar yetkili olursa olsun bir 'administrator'ı doğrudan
    susturmaya izin vermez; bu yüzden önce yetkisini geçici olarak kaldırıyoruz.
    Grup sahibi (creator) hiçbir şekilde susturulamaz, bu Telegram'ın kendi kısıtlaması."""
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
    except Exception as e:
        member = None
        print(f"Üye bilgisi alınamadı: {e}")

    if member and member.status == "creator":
        return False

    if member and member.status == "administrator":
        try:
            await context.bot.promote_chat_member(
                chat_id, user.id,
                can_change_info=False, can_delete_messages=False, can_invite_users=False,
                can_restrict_members=False, can_pin_messages=False, can_promote_members=False,
                can_manage_chat=False, can_manage_video_chats=False,
            )
        except Exception as e:
            print(f"Yetki geçici kaldırma hatası (bot yeterli izne sahip olmayabilir): {e}")

    try:
        await context.bot.restrict_chat_member(
            chat_id, user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        return True
    except Exception as e:
        print(f"Susturma hatası: {e}")
        return False


async def punish_user(context, chat_id, user, reason):
    """Yasaklı kelime, link ihlali için ceza. Uyarı arttıkça mute süresi katlanır. Yetkililer dahil herkese uygulanır."""
    s = get_settings()
    bump_warn_count(chat_id, user.id)
    warns = get_warn_count(chat_id, user.id)
    mute_minutes = min(s["flood_mute_minutes"] * warns, 1440)
    until = datetime.now() + timedelta(minutes=mute_minutes)
    ok = await force_restrict(context, chat_id, user, until)
    try:
        if ok:
            await context.bot.send_message(
                chat_id,
                f"🚨 {user.first_name} {reason} için {mute_minutes} dakika susturuldu. (Uyarı: {warns})"
            )
        else:
            await context.bot.send_message(
                chat_id,
                f"⚠️ {user.first_name} grup sahibi olduğu için susturulamadı, ihlal yine de kaydedildi."
            )
    except Exception as e:
        print(f"Mesaj gönderme hatası: {e}")


async def punish_spam(context, chat_id, user, message_ids):
    """Flood/spam için: tüm son mesajları siler ve sabit 10 dakika susturur. Yetkililer dahil herkese uygulanır."""
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception as e:
            print(f"Spam mesajı silme hatası: {e}")

    bump_warn_count(chat_id, user.id)
    warns = get_warn_count(chat_id, user.id)
    mute_minutes = 10
    until = datetime.now() + timedelta(minutes=mute_minutes)
    ok = await force_restrict(context, chat_id, user, until)
    try:
        if ok:
            await context.bot.send_message(
                chat_id,
                f"🚨 {user.first_name} spam/flood yaptığı için tüm mesajları silindi ve {mute_minutes} dakika susturuldu. (Uyarı: {warns})"
            )
        else:
            await context.bot.send_message(
                chat_id,
                f"⚠️ {user.first_name} grup sahibi olduğu için susturulamadı, spam mesajları yine de silindi."
            )
    except Exception as e:
        print(f"Mesaj gönderme hatası: {e}")


async def check_flood(update, context):
    msg = update.message
    chat_id = msg.chat_id
    user = msg.from_user
    s = get_settings()
    limit, window = s["flood_limit"], s["flood_window"]

    key = (chat_id, user.id)
    now = datetime.now().timestamp()
    entries = [e for e in flood_tracker.get(key, []) if now - e[0] <= window]
    entries.append((now, msg.message_id))
    flood_tracker[key] = entries

    text = (msg.text or msg.caption or "").strip()
    is_dup_spam = False
    dup_ids = []
    if text:
        last = duplicate_tracker.get(key)
        if last and last[0] == text:
            count = last[1] + 1
            ids = last[2] + [msg.message_id]
            duplicate_tracker[key] = (text, count, ids)
            if count >= 3:
                is_dup_spam = True
                dup_ids = ids
        else:
            duplicate_tracker[key] = (text, 1, [msg.message_id])

    if len(entries) < limit and not is_dup_spam:
        return

    message_ids = dup_ids if is_dup_spam else [e[1] for e in entries]
    flood_tracker[key] = []
    duplicate_tracker[key] = ("", 0, [])
    await punish_spam(context, chat_id, user, message_ids)


# ==================== Genel mesaj handler'ı ====================
async def universal_handler(update, context):
    msg = update.message
    if not msg or not msg.from_user:
        return
    if msg.new_chat_members or msg.left_chat_member:
        return
    if msg.from_user.id == context.bot.id:
        return

    log_message(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name)

    # --- sayı tahmin oyunu ---
    if msg.text and msg.text.strip().lstrip("-").isdigit():
        game = guess_get(msg.chat_id)
        if game:
            guess_val = int(msg.text.strip())
            guess_bump(msg.chat_id)
            target = game["target"]
            if guess_val == target:
                guess_end(msg.chat_id)
                bump_xp(msg.chat_id, msg.from_user.id,
                        msg.from_user.username or msg.from_user.first_name, 20)
                await msg.reply_text(
                    f"🎉 Bildin! Doğru sayı {target} idi. Tebrikler {msg.from_user.first_name}! (+20 XP)"
                )
            elif guess_val < target:
                await msg.reply_text("🔼 Daha yüksek")
            else:
                await msg.reply_text("🔽 Daha düşük")
            return

    if msg.chat.type in ("group", "supergroup"):
        # NOT: Aşağıdaki kurallar artık yetkililer dahil HERKESE uygulanır.
        if msg.text and contains_phone_number(msg.text):
            try:
                await msg.delete()
            except Exception as e:
                print(f"Telefon numarası silme hatası: {e}")
            try:
                await context.bot.send_message(
                    msg.chat_id,
                    f"🚫 {msg.from_user.first_name}, telefon numarası paylaşımı yasak. Mesajın silindi."
                )
            except Exception as e:
                print(f"Uyarı gönderme hatası: {e}")
            return

        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        if any(e.type in ("mention", "text_mention") for e in entities):
            try:
                await msg.delete()
            except Exception as e:
                print(f"Etiket silme hatası: {e}")
            return

        if msg.text and LINK_RE.search(msg.text):
            try:
                await msg.delete()
            except Exception as e:
                print(f"Link silme hatası: {e}")
            await punish_user(context, msg.chat_id, msg.from_user, "izinsiz link paylaştığı")
            return

        banned = get_banned_words()
        if banned and msg.text and contains_banned_word(msg.text, banned):
            try:
                await msg.delete()
            except Exception as e:
                print(f"Kelime silme hatası: {e}")
            await punish_user(context, msg.chat_id, msg.from_user, "yasaklı kelime kullandığı")
            return

        await check_flood(update, context)

    if msg.text and not msg.text.startswith("/"):
        bump_xp(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name, 1)

    if msg.photo or msg.sticker or msg.video or msg.animation or msg.video_note:
        try:
            await msg.delete()
            print(f"Silindi → chat {msg.chat_id}, kullanıcı {msg.from_user.id}")
        except Exception as e:
            print(f"Silme hatası: {e}")


# ==================== /id ====================
async def cmd_id(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
        await msg.reply_text(
            f"🆔 Kullanıcı: {target.first_name}\n"
            f"Kullanıcı ID: <code>{target.id}</code>\n"
            f"Kullanıcı adı: @{target.username if target.username else 'yok'}\n"
            f"Sohbet ID: <code>{chat_id}</code>",
            parse_mode="HTML"
        )
    else:
        user = msg.from_user
        await msg.reply_text(
            f"🆔 Senin ID: <code>{user.id}</code>\n"
            f"Sohbet ID: <code>{chat_id}</code>",
            parse_mode="HTML"
        )


# ==================== /bilgi ====================
BILGI_TEXT = (
    "ℹ️ <b>Oyunlar ve XP Rehberi</b>\n\n"
    "<b>XP nasıl kazanılır?</b>\n"
    "• Grupta normal mesaj (komut olmayan) yazmak → +1 XP\n"
    "• 🔢 /sayitahmin — sayıyı bilirsen → +20 XP\n"
    "• 🎰 /slot — 3 sembol tutarsa → +15 XP\n"
    "• 🃏 /blackjack — krupiyeyi yenersen → +20 XP (ilk elde 21 yaparsan +25 XP)\n\n"
    "<b>Seviye sistemi</b>\n"
    "Her 100 XP bir seviye. /seviye yazarak kendi (veya reply attığın kişinin) "
    "XP'sini ve seviyesini görebilirsin. /liderlik ile grubun sıralamasına bakabilirsin.\n\n"
    "<b>Oyunlar</b>\n"
    "🎮 <code>/xox</code> — birinin mesajına reply atıp yaz, XOX (tic-tac-toe) başlar\n"
    "🤖 <code>/xox ai</code> — yapay zekaya karşı XOX\n"
    "🔚 <code>/xoxbitir</code> — devam eden XOX'u iptal eder\n"
    "🎲 <code>/zar</code> — zar atar\n"
    "🪙 <code>/yazitura</code> — yazı tura atar\n"
    "🎰 <code>/slot</code> — slot makinesi çevirir\n"
    "🃏 <code>/blackjack</code> — krupiyeye karşı blackjack başlatır (Kart Çek / Dur butonlarıyla oynanır)\n"
    "🔚 <code>/blackjackbitir</code> — devam eden eli iptal eder\n"
    "🔢 <code>/sayitahmin</code> — 1-100 arası sayı tahmin oyunu başlatır\n"
    "🔚 <code>/sayitahminbitir</code> — sayı tahmin oyununu iptal eder\n\n"
    "<b>Diğer</b>\n"
    "🆔 <code>/id</code> — kendi ID'ni (veya reply attığın kişinin ID'sini) gösterir\n"
    "📊 <code>/gunluk</code>, <code>/aylik</code> — mesaj istatistikleri"
)


async def cmd_bilgi(update, context):
    await update.message.reply_text(BILGI_TEXT, parse_mode="HTML")


# ==================== /mute ====================
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


# ==================== /unmute ====================
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


# ==================== Moderasyon: /kick /ban /unban ====================
async def cmd_kick(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if msg.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(context, chat_id, msg.from_user.id):
        await msg.reply_text("Bu komutu sadece adminler kullanabilir.")
        return
    if not msg.reply_to_message:
        await msg.reply_text("Atmak istediğin kişinin mesajına reply atıp /kick yaz.")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id)
        await msg.reply_text(f"👢 {target.first_name} gruptan atıldı (tekrar katılabilir).")
    except Exception as e:
        await msg.reply_text(f"İşlem başarısız: {e}")


async def cmd_ban(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if msg.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(context, chat_id, msg.from_user.id):
        await msg.reply_text("Bu komutu sadece adminler kullanabilir.")
        return
    if not msg.reply_to_message:
        await msg.reply_text("Yasaklamak istediğin kişinin mesajına reply atıp /ban yaz.")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await msg.reply_text(f"⛔ {target.first_name} kalıcı olarak yasaklandı.")
    except Exception as e:
        await msg.reply_text(f"İşlem başarısız: {e}")


async def cmd_unban(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if msg.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(context, chat_id, msg.from_user.id):
        await msg.reply_text("Bu komutu sadece adminler kullanabilir.")
        return
    if not context.args:
        await msg.reply_text("Kullanım: /unban <kullanıcı_id>")
        return
    try:
        uid = int(context.args[0])
        await context.bot.unban_chat_member(chat_id, uid)
        await msg.reply_text(f"✅ {uid} numaralı kullanıcının yasağı kaldırıldı.")
    except Exception as e:
        await msg.reply_text(f"İşlem başarısız: {e}")


# ==================== /gunluk ve /aylik ====================
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


# ==================== XP / Seviye ====================
async def cmd_seviye(update, context):
    msg = update.message
    target = msg.reply_to_message.from_user if msg.reply_to_message else msg.from_user
    xp = get_xp(msg.chat_id, target.id)
    lvl = level_from_xp(xp)
    kalan = xp_to_next(xp)
    await msg.reply_text(f"📊 {target.first_name}\nSeviye: {lvl}\nXP: {xp}\nSonraki seviyeye: {kalan} XP")


async def cmd_liderlik(update, context):
    rows = get_leaderboard(update.message.chat_id)
    if not rows:
        await update.message.reply_text("Henüz kimse XP kazanmadı.")
        return
    lines = ["🏆 XP Liderlik Tablosu", ""]
    for i, (username, xp) in enumerate(rows, 1):
        lines.append(f"{i}. {username or 'Bilinmeyen'} — {xp} XP (Seviye {level_from_xp(xp)})")
    await update.message.reply_text("\n".join(lines))


# ==================== Mini oyunlar ====================
async def cmd_zar(update, context):
    await context.bot.send_dice(chat_id=update.message.chat_id, emoji="🎲")


async def cmd_yazitura(update, context):
    sonuc = random.choice(["🪙 Yazı!", "🪙 Tura!"])
    await update.message.reply_text(sonuc)


async def cmd_sayitahmin(update, context):
    msg = update.message
    if guess_get(msg.chat_id):
        await msg.reply_text("Zaten devam eden bir sayı tahmin oyunu var. Bitirmek için /sayitahminbitir yaz.")
        return
    target = random.randint(1, 100)
    guess_start(msg.chat_id, target)
    await msg.reply_text("🔢 1 ile 100 arasında bir sayı tuttum! Tahminini mesaj olarak yaz.")


async def cmd_sayitahmin_bitir(update, context):
    chat_id = update.message.chat_id
    game = guess_get(chat_id)
    if game:
        guess_end(chat_id)
        await update.message.reply_text(f"Oyun iptal edildi. Doğru sayı {game['target']} idi.")
    else:
        await update.message.reply_text("Devam eden bir sayı tahmin oyunu yok.")


# ==================== /slot ====================
SLOT_JACKPOT_VALUES = (1, 22, 43, 64)  # Telegram'ın 🎰 dice değerlerinde 3 sembol eşleşmesi


async def cmd_slot(update, context):
    msg = update.message
    dice_msg = await context.bot.send_dice(chat_id=msg.chat_id, emoji="🎰")
    await asyncio.sleep(2.2)
    value = dice_msg.dice.value
    if value in SLOT_JACKPOT_VALUES:
        bump_xp(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name, 15)
        await msg.reply_text(f"🎉 JACKPOT! {msg.from_user.first_name} 3 sembol tuttu! (+15 XP)")
    else:
        await msg.reply_text("😅 Tutmadı, tekrar dene!")


# ==================== /blackjack ====================
SUITS = ["♠️", "♥️", "♦️", "♣️"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck():
    deck = [(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def card_value(rank):
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)


def hand_value(hand):
    total = sum(card_value(r) for r, s in hand)
    aces = sum(1 for r, s in hand if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def hand_str(hand):
    return " ".join(f"{r}{s}" for r, s in hand)


def bj_keyboard(chat_id, user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🃏 Kart Çek", callback_data=f"bj:{chat_id}:{user_id}:hit"),
        InlineKeyboardButton("✋ Dur", callback_data=f"bj:{chat_id}:{user_id}:stand"),
    ]])


def bj_status_text(player, dealer, dealer_hidden=True):
    if dealer_hidden:
        dealer_str = f"{dealer[0][0]}{dealer[0][1]} 🂠"
        dealer_val = "?"
    else:
        dealer_str = hand_str(dealer)
        dealer_val = hand_value(dealer)
    return (f"🃏 Blackjack\n\n"
            f"Krupiye: {dealer_str} (Toplam: {dealer_val})\n"
            f"Sen: {hand_str(player)} (Toplam: {hand_value(player)})")


async def cmd_blackjack(update, context):
    msg = update.message
    key = (msg.chat_id, msg.from_user.id)
    if key in blackjack_games:
        await msg.reply_text("Zaten devam eden bir blackjack elin var. Bitirmek için /blackjackbitir yaz.")
        return
    deck = new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    blackjack_games[key] = {"deck": deck, "player": player, "dealer": dealer}

    if hand_value(player) == 21:
        del blackjack_games[key]
        bump_xp(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name, 25)
        await msg.reply_text(
            f"🎉 BLACKJACK! {msg.from_user.first_name} ilk elde 21 yaptı! (+25 XP)\n\n"
            f"Senin elin: {hand_str(player)}\nKrupiye: {hand_str(dealer)}"
        )
        return

    await msg.reply_text(bj_status_text(player, dealer), reply_markup=bj_keyboard(msg.chat_id, msg.from_user.id))


async def cmd_blackjack_bitir(update, context):
    key = (update.message.chat_id, update.message.from_user.id)
    if key in blackjack_games:
        del blackjack_games[key]
        await update.message.reply_text("Blackjack eli iptal edildi.")
    else:
        await update.message.reply_text("Devam eden bir blackjack elin yok.")


async def bj_finish(query, key, player, dealer, deck, chat_id, user_id, first_name, username):
    while hand_value(dealer) < 17:
        dealer.append(deck.pop())

    pv, dv = hand_value(player), hand_value(dealer)
    if pv > 21:
        result = f"💥 Battın! ({pv}) Krupiye kazandı."
    elif dv > 21:
        bump_xp(chat_id, user_id, username or first_name, 20)
        result = f"🎉 Krupiye battı ({dv})! Kazandın! (+20 XP)"
    elif pv > dv:
        bump_xp(chat_id, user_id, username or first_name, 20)
        result = f"🎉 Kazandın! Sen: {pv} · Krupiye: {dv} (+20 XP)"
    elif pv < dv:
        result = f"😔 Kaybettin. Sen: {pv} · Krupiye: {dv}"
    else:
        result = f"🤝 Berabere! ({pv})"

    del blackjack_games[key]
    text = f"🃏 Blackjack — Sonuç\n\nSen: {hand_str(player)} ({pv})\nKrupiye: {hand_str(dealer)} ({dv})\n\n{result}"
    await query.edit_message_text(text)


async def bj_callback(update, context):
    query = update.callback_query
    try:
        _, chat_id_s, user_id_s, action = query.data.split(":")
        chat_id, user_id = int(chat_id_s), int(user_id_s)
    except Exception:
        await query.answer()
        return

    if query.from_user.id != user_id:
        await query.answer("Bu senin elin değil!", show_alert=True)
        return

    key = (chat_id, user_id)
    game = blackjack_games.get(key)
    if not game:
        await query.answer("Bu oyun artık aktif değil.", show_alert=True)
        return

    deck, player, dealer = game["deck"], game["player"], game["dealer"]

    if action == "hit":
        player.append(deck.pop())
        if hand_value(player) > 21:
            await bj_finish(query, key, player, dealer, deck, chat_id, user_id,
                             query.from_user.first_name, query.from_user.username)
            await query.answer()
            return
        await query.edit_message_text(bj_status_text(player, dealer), reply_markup=bj_keyboard(chat_id, user_id))
        await query.answer()
        return

    if action == "stand":
        await bj_finish(query, key, player, dealer, deck, chat_id, user_id,
                         query.from_user.first_name, query.from_user.username)
        await query.answer()
        return

    await query.answer()


# ==================== XOX (tic-tac-toe) ====================
WIN_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def check_winner(board):
    for a, b, c in WIN_LINES:
        if board[a] != "_" and board[a] == board[b] == board[c]:
            return board[a]
    if "_" not in board:
        return "draw"
    return None


def minimax(board, current, ai_player):
    winner = check_winner(board)
    opp = "O" if ai_player == "X" else "X"
    if winner == ai_player:
        return 1, None
    if winner == opp:
        return -1, None
    if winner == "draw":
        return 0, None

    moves = [i for i, v in enumerate(board) if v == "_"]
    best_score = None
    best_move = moves[0]
    for m in moves:
        nb = board[:m] + current + board[m + 1:]
        score, _ = minimax(nb, "O" if current == "X" else "X", ai_player)
        if current == ai_player:
            if best_score is None or score > best_score:
                best_score, best_move = score, m
        else:
            if best_score is None or score < best_score:
                best_score, best_move = score, m
    return best_score, best_move


def ai_move(board, ai_player):
    _, move = minimax(board, ai_player, ai_player)
    return move


def xox_new_game(chat_id, x_id, x_name, o_id, o_name, vs_ai):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR REPLACE INTO xox_games "
        "(chat_id, player_x, player_x_name, player_o, player_o_name, board, turn, vs_ai, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (chat_id, x_id, x_name, o_id, o_name, "_________", "X", int(vs_ai), "active"),
    )
    conn.commit()
    conn.close()


def xox_get(chat_id):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM xox_games WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def xox_update_board(chat_id, board, turn):
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE xox_games SET board=?, turn=? WHERE chat_id=?", (board, turn, chat_id))
    conn.commit()
    conn.close()


def xox_end(chat_id):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM xox_games WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def xox_keyboard(chat_id, board):
    symbols = {"X": "❌", "O": "⭕", "_": "▫️"}
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            row.append(InlineKeyboardButton(symbols[board[i]], callback_data=f"xox:{chat_id}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def xox_status_text(game, board):
    turn_name = game["player_x_name"] if game["turn"] == "X" else game["player_o_name"]
    symbol = "❌" if game["turn"] == "X" else "⭕"
    return (f"🎮 {game['player_x_name']} (❌) vs {game['player_o_name']} (⭕)\n"
            f"Sıra: {turn_name} {symbol}")


def xox_result_text(game, winner):
    if winner == "draw":
        return f"🤝 Berabere! {game['player_x_name']} (❌) vs {game['player_o_name']} (⭕)"
    name = game["player_x_name"] if winner == "X" else game["player_o_name"]
    symbol = "❌" if winner == "X" else "⭕"
    return f"🏆 Kazandı: {name} {symbol}\n{game['player_x_name']} (❌) vs {game['player_o_name']} (⭕)"


async def cmd_xox(update, context):
    msg = update.message
    chat_id = msg.chat_id
    if xox_get(chat_id):
        await msg.reply_text("Bu sohbette zaten devam eden bir XOX oyunu var. Bitirmek için /xoxbitir yaz.")
        return

    args = context.args
    if args and args[0].lower() in ("ai", "yapayzeka", "bot"):
        xox_new_game(chat_id, msg.from_user.id, msg.from_user.first_name, None, "Yapay Zeka", True)
        board = "_________"
        await msg.reply_text(
            f"🎮 XOX başladı! {msg.from_user.first_name} (❌) vs Yapay Zeka (⭕)\nSıra: {msg.from_user.first_name} ❌",
            reply_markup=xox_keyboard(chat_id, board)
        )
        return

    if msg.reply_to_message and msg.reply_to_message.from_user:
        opponent = msg.reply_to_message.from_user
        if opponent.id == msg.from_user.id:
            await msg.reply_text("Kendinle oynayamazsın 😄 Yapay zekaya karşı oynamak için /xox ai yaz.")
            return
        if opponent.is_bot:
            await msg.reply_text("Bir bota reply atarak oynayamazsın, yapay zeka için /xox ai yaz.")
            return
        xox_new_game(chat_id, msg.from_user.id, msg.from_user.first_name, opponent.id, opponent.first_name, False)
        board = "_________"
        await msg.reply_text(
            f"🎮 XOX başladı! {msg.from_user.first_name} (❌) vs {opponent.first_name} (⭕)\n"
            f"Sıra: {msg.from_user.first_name} ❌",
            reply_markup=xox_keyboard(chat_id, board)
        )
        return

    await msg.reply_text(
        "XOX nasıl oynanır:\n"
        "• Birine karşı oynamak için onun mesajına reply atıp /xox yaz\n"
        "• Yapay zekaya karşı oynamak için /xox ai yaz\n"
        "• Oyunu iptal etmek için /xoxbitir yaz"
    )


async def cmd_xox_bitir(update, context):
    chat_id = update.message.chat_id
    if xox_get(chat_id):
        xox_end(chat_id)
        await update.message.reply_text("XOX oyunu iptal edildi.")
    else:
        await update.message.reply_text("Şu an devam eden bir XOX oyunu yok.")


async def xox_callback(update, context):
    query = update.callback_query
    try:
        _, chat_id_s, pos_s = query.data.split(":")
        chat_id, pos = int(chat_id_s), int(pos_s)
    except Exception:
        await query.answer()
        return

    game = xox_get(chat_id)
    if not game:
        await query.answer("Bu oyun artık aktif değil.", show_alert=True)
        return

    user = query.from_user
    turn = game["turn"]
    expected_id = game["player_x"] if turn == "X" else game["player_o"]

    if turn == "O" and game["vs_ai"]:
        await query.answer("Şu an yapay zekanın sırası.", show_alert=True)
        return
    if user.id != expected_id:
        await query.answer("Sıra sende değil!", show_alert=True)
        return

    board = game["board"]
    if board[pos] != "_":
        await query.answer("Burası dolu!", show_alert=True)
        return

    board = board[:pos] + turn + board[pos + 1:]
    winner = check_winner(board)
    if winner:
        xox_end(chat_id)
        await query.edit_message_text(xox_result_text(game, winner), reply_markup=xox_keyboard(chat_id, board))
        await query.answer()
        return

    next_turn = "O" if turn == "X" else "X"

    if game["vs_ai"] and next_turn == "O":
        ai_pos = ai_move(board, "O")
        if ai_pos is not None:
            board = board[:ai_pos] + "O" + board[ai_pos + 1:]
        winner = check_winner(board)
        if winner:
            xox_end(chat_id)
            await query.edit_message_text(xox_result_text(game, winner), reply_markup=xox_keyboard(chat_id, board))
            await query.answer()
            return
        next_turn = "X"

    xox_update_board(chat_id, board, next_turn)
    game["turn"] = next_turn
    await query.edit_message_text(xox_status_text(game, board), reply_markup=xox_keyboard(chat_id, board))
    await query.answer()


# ==================== Web panel ====================
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
        textarea, input, select {{ width: 100%; padding: 11px; border: 1px solid #333; border-radius: 8px; background: #111; color: #fff; margin-bottom: 10px; font-size: 0.95rem; }}
        textarea {{ min-height: 90px; resize: vertical; }}
        .row {{ display: flex; gap: 8px; }}
        .row > * {{ flex: 1; }}
        button {{ background: #2aabee; color: #fff; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
        button.danger {{ background: #e74c3c; }}
        button.start {{ background: #2ecc71; }}
        button.stop {{ background: #f39c12; }}
        .status {{ background: #143d2a; color: #2ecc71; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 10px; display: inline-block; }}
        .status2 {{ background: #3d2a14; color: #f39c12; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 10px; margin-left: 8px; display: inline-block; }}
        .status3 {{ background: #1e2a3d; color: #5dade2; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 18px; display: inline-block; }}
        .chat-item {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #2a2a2a; gap: 8px; }}
        .chat-id {{ font-family: monospace; color: #2aabee; }}
        .chat-actions {{ display: flex; gap: 6px; }}
        .chat-actions form {{ margin: 0; }}
        .chat-actions button {{ padding: 6px 10px; font-size: 0.8rem; }}
        .empty {{ color: #555; font-size: 0.9rem; }}
        .photo-preview {{ width: 100%; max-width: 260px; border-radius: 8px; margin-bottom: 10px; display: block; }}
        .file-input {{ background: #111; border: 1px dashed #333; padding: 10px; }}
        code {{ background: #111; padding: 2px 6px; border-radius: 4px; color: #2aabee; }}
        .checkline {{ display: flex; align-items: center; gap: 8px; margin: 6px 0 14px; font-size: 0.9rem; }}
        .checkline input {{ width: auto; margin: 0; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Bot Yönetim Paneli</h1>
    <p class="sub">Mesaj, aralık, grup, oyun ve koruma ayarlarını buradan yönet</p>
    <div class="status">● Sistem çalışıyor · Aralık: {interval_value} {interval_unit}</div>
    <div class="status2">🛡 Flood + spam + kelime koruması aktif (yetkililer dahil)</div>

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
        <h2>Gönderim Aralığı</h2>
        <form method="post" action="/set_interval">
            <div class="row">
                <input type="number" step="any" name="value" value="{interval_value}" min="0.001" required>
                <select name="unit">
                    <option value="ms" {sel_ms}>Milisaniye</option>
                    <option value="sn" {sel_sn}>Saniye</option>
                    <option value="dk" {sel_dk}>Dakika</option>
                </select>
            </div>
            <button type="submit">Aralığı Güncelle</button>
        </form>
    </div>

    <div class="card">
        <h2>Flood / Spam Koruması</h2>
        <form method="post" action="/set_flood">
            <div class="row">
                <div>
                    <label style="font-size:0.8rem;color:#999">Kaç mesaj</label>
                    <input type="number" name="limit" value="{flood_limit}" min="2">
                </div>
                <div>
                    <label style="font-size:0.8rem;color:#999">Kaç saniyede</label>
                    <input type="number" name="window" value="{flood_window}" min="1">
                </div>
                <div>
                    <label style="font-size:0.8rem;color:#999">Link/kelime mute (dk, katlanır)</label>
                    <input type="number" name="mute" value="{flood_mute}" min="1">
                </div>
            </div>
            <div class="checkline">
                <input type="checkbox" name="link_block" id="lb" {link_checked}>
                <label for="lb">Link paylaşımını otomatik sil (yetkililer dahil herkese uygulanır)</label>
            </div>
            <button type="submit">Ayarları Kaydet</button>
        </form>
        <p style="font-size:0.8rem;color:#666;margin-top:8px">
            Flood/spam tespit edildiğinde kişinin son tüm mesajları silinir ve sabit <b>10 dakika</b> susturulur
            (bu süre değiştirilemez, yukarıdaki mute alanı sadece link/kelime ihlalleri içindir). Yetkililer dahil
            herkese uygulanır — sadece grup sahibi (creator) Telegram kısıtlaması nedeniyle susturulamaz.
        </p>
    </div>

    <div class="card">
        <h2>Yasaklı Kelimeler</h2>
        <form method="post" action="/set_banned_words">
            <textarea name="words" placeholder="her satıra bir kelime">{banned_words}</textarea>
            <button type="submit">Kaydet</button>
        </form>
        <p style="font-size:0.8rem;color:#666;margin-top:8px">
            Bu kelimeleri içeren mesajlar (yetkililer dahil herkes) otomatik silinir ve gönderen susturulmaya çalışılır.
        </p>
    </div>

    <div class="card">
        <h2>Manuel XP Ver</h2>
        <form method="post" action="/set_xp">
            <div class="row">
                <div>
                    <label style="font-size:0.8rem;color:#999">Sohbet (Grup) ID</label>
                    <input type="text" name="chat_id" placeholder="-1001234567890" required>
                </div>
                <div>
                    <label style="font-size:0.8rem;color:#999">Kullanıcı ID</label>
                    <input type="text" name="user_id" placeholder="123456789" required>
                </div>
            </div>
            <div class="row">
                <div>
                    <label style="font-size:0.8rem;color:#999">Kullanıcı adı (opsiyonel)</label>
                    <input type="text" name="username" placeholder="@kullanici">
                </div>
                <div>
                    <label style="font-size:0.8rem;color:#999">XP miktarı</label>
                    <input type="number" name="amount" value="100" required>
                </div>
            </div>
            <div class="row">
                <button type="submit" name="mode" value="add">XP Ekle</button>
                <button type="submit" name="mode" value="set" style="background:#8e44ad">XP'yi Bu Değere Sabitle</button>
            </div>
        </form>
        <p style="font-size:0.8rem;color:#666;margin-top:8px">
            Kullanıcı ID'sini öğrenmek için gruptaki kullanıcı bota <code>/id</code> yazabilir (birine reply atarsa onun ID'sini gösterir).
        </p>
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
        <h2>Komut Listesi</h2>
        <p style="font-size:0.85rem;color:#999;line-height:1.9">
            <b>Genel</b><br>
            <code>/id</code> — kendi ID'ni (veya reply attığın kişinin ID'sini) gösterir<br>
            <code>/bilgi</code> — oyunlar ve XP nasıl kazanılır rehberi<br><br>
            <b>Moderasyon (admin)</b><br>
            <code>/mute 5dakika</code> — reply atılan kişiyi susturur<br>
            <code>/unmute</code> — susturmayı kaldırır<br>
            <code>/kick</code> — reply atılan kişiyi gruptan atar (tekrar girebilir)<br>
            <code>/ban</code> — reply atılan kişiyi kalıcı yasaklar<br>
            <code>/unban &lt;kullanıcı_id&gt;</code> — yasağı kaldırır<br><br>
            <b>İstatistik</b><br>
            <code>/gunluk</code> — bugünkü mesaj istatistikleri<br>
            <code>/aylik</code> — bu ayki mesaj istatistikleri<br>
            <code>/seviye</code> — kendi (veya reply attığın kişinin) XP/seviyesi<br>
            <code>/liderlik</code> — grubun XP sıralaması<br><br>
            <b>Oyunlar</b><br>
            <code>/xox</code> — birine reply atıp yazarsan o kişiyle XOX başlatır<br>
            <code>/xox ai</code> — yapay zekaya karşı XOX başlatır<br>
            <code>/xoxbitir</code> — devam eden XOX oyununu iptal eder<br>
            <code>/zar</code> — zar atar<br>
            <code>/yazitura</code> — yazı tura atar<br>
            <code>/slot</code> — slot makinesi çevirir, 3 sembol tutarsa +15 XP<br>
            <code>/blackjack</code> — krupiyeye karşı blackjack başlatır<br>
            <code>/blackjackbitir</code> — devam eden blackjack elini iptal eder<br>
            <code>/sayitahmin</code> — 1-100 arası sayı tahmin oyunu başlatır<br>
            <code>/sayitahminbitir</code> — sayı tahmin oyununu iptal eder<br><br>
            Flood/spam: {flood_limit} mesaj / {flood_window} sn → tüm son mesajlar silinir + sabit 10 dakika susturma (yetkililer dahil, grup sahibi hariç)<br>
            📵 Telefon numarası paylaşımı otomatik silinir + uyarı mesajı atılır (herkese)<br>
            🏷 @etiket / mention içeren mesajlar otomatik silinir (herkese, uyarısız)<br>
            🎬 Fotoğraf, sticker, video, GIF ve video not otomatik silinir
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
    s = get_settings()
    chats = get_chats()

    if chats:
        chat_html = ""
        for c in chats:
            cid, title, added, active = c["chat_id"], c["title"], c["added_at"], c["active"]
            badge = '<span style="color:#2ecc71">● Aktif</span>' if active else '<span style="color:#f39c12">● Durduruldu</span>'
            btn_label = "Durdur" if active else "Başla"
            btn_class = "stop" if active else "start"
            chat_html += f"""
            <div class="chat-item">
                <div>
                    <div class="chat-id">{cid}</div>
                    <div style="font-size:0.8rem;color:#666">{title or 'İsimsiz'} · {added[:16]} · {badge}</div>
                </div>
                <div class="chat-actions">
                    <form method="post" action="/toggle_chat">
                        <input type="hidden" name="chat_id" value="{cid}">
                        <button type="submit" class="{btn_class}">{btn_label}</button>
                    </form>
                    <form method="post" action="/remove_chat">
                        <input type="hidden" name="chat_id" value="{cid}">
                        <button type="submit" class="danger">Sil</button>
                    </form>
                </div>
            </div>"""
    else:
        chat_html = '<p class="empty">Henüz grup yok</p>'

    if s["photo_path"] and os.path.exists(s["photo_path"]):
        photo_preview = '<img class="photo-preview" src="/photo">'
        remove_photo_form = """
        <form method="post" action="/remove_photo" style="margin-top:8px">
            <button type="submit" class="danger">Fotoğrafı Kaldır</button>
        </form>"""
    else:
        photo_preview = '<p class="empty" style="margin-bottom:10px">Fotoğraf yok · sadece metin gönderilecek</p>'
        remove_photo_form = ""

    unit = s["interval_unit"] or "sn"
    return HTML.format(
        message=s["message"],
        interval_value=s["interval_value"],
        interval_unit=unit,
        sel_ms="selected" if unit == "ms" else "",
        sel_sn="selected" if unit == "sn" else "",
        sel_dk="selected" if unit == "dk" else "",
        chat_count=len(chats),
        chat_list=chat_html,
        photo_preview=photo_preview,
        remove_photo_form=remove_photo_form,
        flood_limit=s["flood_limit"],
        flood_window=s["flood_window"],
        flood_mute=s["flood_mute_minutes"],
        link_checked="checked" if s["link_block"] else "",
        banned_words="\n".join(get_banned_words()),
    )


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
async def set_interval(value: float = Form(...), unit: str = Form(...)):
    if unit not in ("ms", "sn", "dk"):
        unit = "sn"
    update_interval(max(0.001, value), unit)
    restart_scheduler()
    return RedirectResponse("/", status_code=303)


@app.post("/set_flood")
async def set_flood_web(limit: int = Form(...), window: int = Form(...), mute: int = Form(...),
                         link_block: str = Form(None)):
    update_flood(max(2, limit), max(1, window), max(1, mute), 1 if link_block else 0)
    return RedirectResponse("/", status_code=303)


@app.post("/set_banned_words")
async def set_banned_words_web(words: str = Form("")):
    set_banned_words(words.splitlines())
    return RedirectResponse("/", status_code=303)


@app.post("/set_xp")
async def set_xp_web(chat_id: str = Form(...), user_id: str = Form(...),
                      username: str = Form(""), amount: int = Form(...), mode: str = Form("add")):
    try:
        cid = int(chat_id.strip())
        uid = int(user_id.strip())
        uname = username.strip().lstrip("@") or None
        if mode == "set":
            set_xp_absolute(cid, uid, uname, amount)
        else:
            bump_xp(cid, uid, uname, amount)
    except Exception as e:
        print(f"XP verme hatası: {e}")
    return RedirectResponse("/", status_code=303)


@app.post("/add_chat")
async def add_chat_web(chat_id: str = Form(...)):
    try:
        add_chat(int(chat_id.strip()))
    except Exception:
        pass
    return RedirectResponse("/", status_code=303)


@app.post("/remove_chat")
async def remove_chat_web(chat_id: int = Form(...)):
    remove_chat(chat_id)
    return RedirectResponse("/", status_code=303)


@app.post("/toggle_chat")
async def toggle_chat_web(chat_id: int = Form(...)):
    toggle_chat(chat_id)
    return RedirectResponse("/", status_code=303)


async def main():
    global application
    if not TOKEN:
        print("HATA: BOT_TOKEN yok!")
        return
    init_db()
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("bilgi", cmd_bilgi))
    application.add_handler(CommandHandler("mute", cmd_mute))
    application.add_handler(CommandHandler("unmute", cmd_unmute))
    application.add_handler(CommandHandler("kick", cmd_kick))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))
    application.add_handler(CommandHandler("gunluk", cmd_gunluk))
    application.add_handler(CommandHandler("aylik", cmd_aylik))
    application.add_handler(CommandHandler("seviye", cmd_seviye))
    application.add_handler(CommandHandler("liderlik", cmd_liderlik))
    application.add_handler(CommandHandler("zar", cmd_zar))
    application.add_handler(CommandHandler("yazitura", cmd_yazitura))
    application.add_handler(CommandHandler("slot", cmd_slot))
    application.add_handler(CommandHandler("blackjack", cmd_blackjack))
    application.add_handler(CommandHandler("blackjackbitir", cmd_blackjack_bitir))
    application.add_handler(CommandHandler("sayitahmin", cmd_sayitahmin))
    application.add_handler(CommandHandler("sayitahminbitir", cmd_sayitahmin_bitir))
    application.add_handler(CommandHandler("xox", cmd_xox))
    application.add_handler(CommandHandler("xoxbitir", cmd_xox_bitir))
    application.add_handler(CallbackQueryHandler(xox_callback, pattern=r"^xox:"))
    application.add_handler(CallbackQueryHandler(bj_callback, pattern=r"^bj:"))
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
