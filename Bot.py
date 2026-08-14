import asyncio
import random
import sqlite3
import os
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters as tg_filters
)
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8000))
DB = "bot.db"

blackjack_games = {}     # {(chat_id, user_id): {...}}


# ==================== DB ====================
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    ts TEXT
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
    conn.commit()
    conn.close()


def log_message(chat_id, user_id, username):
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO message_log (chat_id, user_id, username, ts) VALUES (?,?,?,?)",
                 (chat_id, user_id, username, datetime.now().isoformat()))
    conn.commit()
    conn.close()


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


# ==================== Genel mesaj handler'ı (loglama + XP + sayı tahmin) ====================
async def universal_handler(update, context):
    msg = update.message
    if not msg or not msg.from_user:
        return
    if msg.from_user.id == context.bot.id:
        return

    log_message(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name)

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

    if msg.text and not msg.text.startswith("/"):
        bump_xp(msg.chat_id, msg.from_user.id, msg.from_user.username or msg.from_user.first_name, 1)


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
    "📊 <code>/gunluk</code>, <code>/aylik</code> — mesaj istatistikleri"
)


async def cmd_bilgi(update, context):
    await update.message.reply_text(BILGI_TEXT, parse_mode="HTML")


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
        input {{ width: 100%; padding: 11px; border: 1px solid #333; border-radius: 8px; background: #111; color: #fff; margin-bottom: 10px; font-size: 0.95rem; }}
        .row {{ display: flex; gap: 8px; }}
        .row > * {{ flex: 1; }}
        button {{ background: #2aabee; color: #fff; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }}
        .status {{ background: #143d2a; color: #2ecc71; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 18px; display: inline-block; }}
        .lb-item {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #2a2a2a; font-size: 0.9rem; }}
        .empty {{ color: #555; font-size: 0.9rem; }}
        label {{ font-size: 0.8rem; color: #999; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Bot Yönetim Paneli</h1>
    <p class="sub">Oyunlar · XP · Mesaj istatistikleri</p>
    <div class="status">● Bot çalışıyor</div>

    <div class="card">
        <h2>Manuel XP Ver</h2>
        <form method="post" action="/set_xp">
            <div class="row">
                <div>
                    <label>Sohbet (Grup) ID</label>
                    <input type="text" name="chat_id" placeholder="-1001234567890" required>
                </div>
                <div>
                    <label>Kullanıcı ID</label>
                    <input type="text" name="user_id" placeholder="123456789" required>
                </div>
            </div>
            <div class="row">
                <div>
                    <label>Kullanıcı adı (opsiyonel)</label>
                    <input type="text" name="username" placeholder="@kullanici">
                </div>
                <div>
                    <label>XP miktarı</label>
                    <input type="number" name="amount" value="100" required>
                </div>
            </div>
            <div class="row">
                <button type="submit" name="mode" value="add">XP Ekle</button>
                <button type="submit" name="mode" value="set" style="background:#8e44ad">XP'yi Bu Değere Sabitle</button>
            </div>
        </form>
    </div>

    <div class="card">
        <h2>Grup Bazlı XP Liderlik Tablosu</h2>
        <form method="get" action="/">
            <input type="text" name="chat_id" placeholder="Sohbet ID gir (-100...)" value="{queried_chat}">
            <button type="submit">Göster</button>
        </form>
        {leaderboard_html}
    </div>

    <div class="card">
        <h2>Mesaj İstatistikleri (bugün)</h2>
        {stats_html}
    </div>

    <div class="card">
        <h2>Komut Listesi</h2>
        <p style="font-size:0.85rem;color:#999;line-height:1.9">
            <b>Oyunlar</b><br>
            <code>/xox</code> · <code>/xox ai</code> · <code>/xoxbitir</code><br>
            <code>/zar</code> · <code>/yazitura</code> · <code>/slot</code><br>
            <code>/blackjack</code> · <code>/blackjackbitir</code><br>
            <code>/sayitahmin</code> · <code>/sayitahminbitir</code><br><br>
            <b>XP / İstatistik</b><br>
            <code>/seviye</code> — kendi (veya reply attığın kişinin) XP/seviyesi<br>
            <code>/liderlik</code> — grubun XP sıralaması<br>
            <code>/gunluk</code> — bugünkü mesaj istatistikleri<br>
            <code>/aylik</code> — bu ayki mesaj istatistikleri<br>
            <code>/bilgi</code> — oyunlar ve XP rehberi
        </p>
    </div>
</div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index(chat_id: str = ""):
    leaderboard_html = '<p class="empty">Yukarıya bir sohbet ID gir</p>'
    stats_html = '<p class="empty">Yukarıya bir sohbet ID gir</p>'

    if chat_id.strip():
        try:
            cid = int(chat_id.strip())
            rows = get_leaderboard(cid, limit=15)
            if rows:
                lb = ""
                for i, (username, xp) in enumerate(rows, 1):
                    lb += f'<div class="lb-item"><span>{i}. {username or "Bilinmeyen"}</span><span>{xp} XP (Sv {level_from_xp(xp)})</span></div>'
                leaderboard_html = lb
            else:
                leaderboard_html = '<p class="empty">Bu sohbette henüz XP yok</p>'

            today = datetime.now().date().isoformat()
            text = stats_text(cid, today, "")
            stats_html = f'<pre style="white-space:pre-wrap;font-size:0.85rem;color:#ccc">{text}</pre>'
        except ValueError:
            leaderboard_html = '<p class="empty">Geçersiz sohbet ID</p>'
            stats_html = '<p class="empty">Geçersiz sohbet ID</p>'

    return HTML.format(
        queried_chat=chat_id,
        leaderboard_html=leaderboard_html,
        stats_html=stats_html,
    )


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
    return RedirectResponse(f"/?chat_id={chat_id}", status_code=303)


# ==================== main ====================
async def main():
    if not TOKEN:
        print("HATA: BOT_TOKEN yok!")
        return
    init_db()
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("bilgi", cmd_bilgi))
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
    application.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, universal_handler))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print(f"Bot hazır → port {PORT}")
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
