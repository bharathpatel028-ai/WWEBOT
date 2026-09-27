#!/usr/bin/env python3
"""
HOMIES WWE CYBER-BRAWL BOT
- 1v1 Grudge Matches
- 2v2 Tag Team Warfare (with live Tag mechanics)
- 4-8 Player Royal Rumble (last wrestler standing)
- Visual Retro HP Bars & Announcer Commentary
- Embedded Keepalive HTTP Server for 24/7 Free Cloud Hosting (Render/Koyeb)
"""

import os
import json
import logging
import random
import io
import asyncio
import re
import threading
from typing import Dict, List, Tuple
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.error import TimedOut, TelegramError, BadRequest, RetryAfter
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# Pillow for card generation
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# ---------------- CONFIG ----------------
DEFAULT_TOKEN = "8949576090:AAEtYg8073-yr4lpRp-6sfaEXX3kEssRywc"
BOT_TOKEN = os.getenv("BOT_TOKEN", DEFAULT_TOKEN)
PERSISTENT_DIR = os.getenv("PERSISTENT_DIR", None)
PORT = int(os.getenv("PORT", 8080))

if PERSISTENT_DIR:
    os.makedirs(PERSISTENT_DIR, exist_ok=True)
STATS_FILE = os.path.join(PERSISTENT_DIR, "user_stats.json") if PERSISTENT_DIR else "user_stats.json"
PARSE_MODE = "HTML"

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------- GAME CONSTANTS ----------------
MAX_HP = 200
MAX_SPECIALS_PER_MATCH = 4
MAX_REVERSALS_PER_MATCH = 3
MAX_NAME_LENGTH = 16

RUMBLE_MIN_PLAYERS = 4
RUMBLE_MAX_PLAYERS = 8
RUMBLE_HP = 200

MOVES = {
    "punch": {"dmg": 10, "emoji": "🥊", "name": "Heavy Punch"},
    "kick": {"dmg": 18, "emoji": "🦵", "name": "Big Boot"},
    "slam": {"dmg": 26, "emoji": "💥", "name": "Body Slam"},
    "dropkick": {"dmg": 34, "emoji": "🚀", "name": "Dropkick"},
    "suplex": {"dmg": 48, "emoji": "🌪️", "name": "German Suplex"},
    "rko": {"dmg": 60, "emoji": "⚡", "name": "Outta Nowhere RKO"},
    "reversal": {"dmg": 0, "emoji": "🛡️", "name": "Counter / Reversal"},
}

# ---------------- STATS PERSISTENCE ----------------
try:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            user_stats: Dict[str, Dict] = json.load(f)
    else:
        user_stats = {}
except Exception:
    logger.exception("Failed to load stats file; starting with empty stats.")
    user_stats = {}

def save_stats():
    try:
        parent = os.path.dirname(STATS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_stats, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save stats")

for k, v in list(user_stats.items()):
    v.setdefault("draws", 0)

# ---------------- IN-MEMORY STATE ----------------
lobbies: Dict[int, Dict] = {}
games: Dict[int, Dict] = {}
rumble_games: Dict[int, Dict] = {}
tag_games: Dict[int, Dict] = {}
rumble_target_choices: Dict[str, Dict] = {}

# ---------------- THEME & VISUAL HELPERS ----------------
def hp_bar(current: int, max_hp: int = MAX_HP, length: int = 10) -> str:
    """Returns a visual retro health bar"""
    current = max(0, current)
    ratio = current / max_hp
    filled = int(round(ratio * length))
    empty = max(0, length - filled)
    if ratio > 0.55:
        icon = "🟩"
    elif ratio > 0.25:
        icon = "🟨"
    else:
        icon = "🟥"
    return f"{icon * filled}{'⬛' * empty} <b>{current}/{max_hp} HP</b>"

def crowd_hype() -> str:
    return random.choice([
        "🔥 <i>The arena roof blows off with roaring cheers!</i>",
        "📣 <i>Fans are standing on their chairs in disbelief!</i>",
        "😱 <i>HOLY SHIT! HOLY SHIT! echoes through the crowd!</i>",
        "⚡ <i>Camera flashes illuminate the electric ringside!</i>",
        "🎙️ <i>ANNOUNCER: 'BAH GAWD! WHAT A MOVE!'</i>"
    ])

async def safe_send(func, *args, **kwargs):
    """Robust wrapper for sending/editing messages."""
    try:
        return await func(*args, **kwargs)
    except TimedOut:
        logger.warning("Telegram request timed out.")
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg or "reply_markup are exactly the same" in msg:
            return None
        logger.warning("BadRequest: %s", e)
    except RetryAfter as e:
        wait = getattr(e, "retry_after", 3)
        await asyncio.sleep(int(wait) + 1)
        try:
            return await func(*args, **kwargs)
        except Exception:
            pass
    except TelegramError as e:
        logger.warning("Telegram error: %s", e)
    except Exception:
        logger.exception("Unexpected send error")
    return None

async def send_short_restriction_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    msg = "— <b>Use another move — you can't use this special or reversal consecutively</b>"
    await safe_send(context.bot.send_message, chat_id=user_id, text=msg, parse_mode=PARSE_MODE)

# ---------------- PIL CARD GENERATORS ----------------
def find_font_pair():
    if not PIL_AVAILABLE:
        return (None, None)
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/Library/Fonts/Arial.ttf"
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return (ImageFont.truetype(p, 54), ImageFont.truetype(p, 26))
            except Exception:
                continue
    try:
        return (ImageFont.load_default(), ImageFont.load_default())
    except Exception:
        return (None, None)

def create_stats_image(name: str, stats: Dict) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W, H = 850, 420
    img = Image.new("RGB", (W, H), color=(15, 12, 28))
    draw = ImageDraw.Draw(img)
    draw.rectangle(((0, 0), (W, 90)), fill=(255, 69, 0))
    draw.text((30, 20), "WWE CAREER PROFILE", font=title_font, fill=(255, 255, 255))
    draw.text((40, 115), f"Wrestler: {name}", font=title_font, fill=(255, 215, 0))
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    draws = stats.get("draws", 0)
    total = wins + losses + draws
    win_pct = round((wins / total) * 100, 1) if total else 0.0
    sp_u = stats.get("specials_used", 0)
    sp_s = stats.get("specials_successful", 0)
    lines = [
        f"🏆 Wins: {wins}    💀 Losses: {losses}    🤝 Draws: {draws}",
        f"📊 Win Percentage: {win_pct}%",
        f"⚡ Specials Landed: {sp_s} / {sp_u} uses",
        "🎖️ Division: Heavyweight Championship Roster"
    ]
    y = 190
    for ln in lines:
        draw.text((40, y), ln, font=body_font, fill=(230, 230, 230))
        y += 40
    draw.text((300, H - 40), "★ WWE CYBER ARENA BRAWLER ★", font=body_font, fill=(120, 120, 150))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

def create_leaderboard_image(entries: List[Tuple[str, int, int, int]]) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W = 850
    rows = max(3, len(entries))
    H = 130 + rows * 44
    img = Image.new("RGB", (W, H), color=(10, 14, 26))
    draw = ImageDraw.Draw(img)
    draw.rectangle(((0, 0), (W, 85)), fill=(30, 144, 255))
    draw.text((30, 18), "WWE HALL OF FAME", font=title_font, fill=(255, 255, 255))
    y = 110
    for i, (name, wins, losses, draws) in enumerate(entries, start=1):
        medal = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else f"{i}. "
        draw.text((40, y), f"{medal}{name}", font=body_font, fill=(255, 255, 255))
        draw.text((W - 320, y), f"{wins}W - {losses}L - {draws}D", font=body_font, fill=(0, 255, 200))
        y += 44
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

# ---------------- KEYBOARD BUILDERS ----------------
def build_1v1_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 Punch (10)", callback_data=f"move|{group_id}|punch"),
            InlineKeyboardButton("🦵 Kick (18)", callback_data=f"move|{group_id}|kick"),
            InlineKeyboardButton("💥 Slam (26)", callback_data=f"move|{group_id}|slam")
        ],
        [
            InlineKeyboardButton("🚀 Dropkick (34)", callback_data=f"move|{group_id}|dropkick"),
            InlineKeyboardButton("🌪️ Suplex (48)", callback_data=f"move|{group_id}|suplex"),
            InlineKeyboardButton("⚡ RKO (60)", callback_data=f"move|{group_id}|rko")
        ],
        [
            InlineKeyboardButton("🛡️ Reversal / Counter", callback_data=f"move|{group_id}|reversal")
        ]
    ])

def build_rumble_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 Punch (10)", callback_data=f"rumble_move|{group_id}|punch"),
            InlineKeyboardButton("🦵 Kick (18)", callback_data=f"rumble_move|{group_id}|kick"),
            InlineKeyboardButton("💥 Slam (26)", callback_data=f"rumble_move|{group_id}|slam")
        ],
        [
            InlineKeyboardButton("🚀 Dropkick (34)", callback_data=f"rumble_move|{group_id}|dropkick"),
            InlineKeyboardButton("🌪️ Suplex (48)", callback_data=f"rumble_move|{group_id}|suplex"),
            InlineKeyboardButton("⚡ RKO (60)", callback_data=f"rumble_move|{group_id}|rko")
        ],
        [
            InlineKeyboardButton("🛡️ Reversal", callback_data=f"rumble_move|{group_id}|reversal")
        ]
    ])

def build_tag_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 Punch (10)", callback_data=f"tag_move|{group_id}|punch"),
            InlineKeyboardButton("🦵 Kick (18)", callback_data=f"tag_move|{group_id}|kick"),
            InlineKeyboardButton("💥 Slam (26)", callback_data=f"tag_move|{group_id}|slam")
        ],
        [
            InlineKeyboardButton("🚀 Dropkick (34)", callback_data=f"tag_move|{group_id}|dropkick"),
            InlineKeyboardButton("🌪️ Suplex (48)", callback_data=f"tag_move|{group_id}|suplex"),
            InlineKeyboardButton("⚡ RKO (60)", callback_data=f"tag_move|{group_id}|rko")
        ],
        [
            InlineKeyboardButton("🔄 Tag Partner", callback_data=f"tag_move|{group_id}|tag"),
            InlineKeyboardButton("🛡️ Reversal", callback_data=f"tag_move|{group_id}|reversal")
        ]
    ])

# ---------------- USER REGISTRATION & COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Please DM me /start to set your wrestler name.")
        return
    uid = str(update.effective_user.id)
    if uid in user_stats and user_stats[uid].get("name"):
        await safe_send(
            update.message.reply_text,
            f"🥊 Welcome back, superstar <b>{user_stats[uid]['name']}</b>!\n"
            f"Use /stats to check your record or invite me to a group to brawl!",
            parse_mode=PARSE_MODE
        )
        return
    user_stats.setdefault(uid, {
        "name": None, "wins": 0, "losses": 0, "draws": 0,
        "specials_used": 0, "specials_successful": 0
    })
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(
        update.message.reply_text,
        f"⚡ <b>WELCOME TO WWE BRAWL!</b>\n\nReply with your ring name (max {MAX_NAME_LENGTH} chars):",
        parse_mode=PARSE_MODE
    )

async def cmd_startcareer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Use /startcareer in DM to change your superstar name.")
        return
    uid = str(update.effective_user.id)
    user_stats.setdefault(uid, {
        "name": None, "wins": 0, "losses": 0, "draws": 0,
        "specials_used": 0, "specials_successful": 0
    })
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"Enter your new superstar name (max {MAX_NAME_LENGTH} chars):")

async def private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_name"):
        name = text.strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            await safe_send(update.message.reply_text, f"⚠️ Name must be 1 to {MAX_NAME_LENGTH} characters. Try again:")
            return
        taken = any(info.get("name") and info["name"].lower() == name.lower() for k, info in user_stats.items() if k != uid)
        if taken:
            await safe_send(update.message.reply_text, "⚠️ That superstar name is already taken! Pick another:")
            return
        user_stats.setdefault(uid, {})
        user_stats[uid].update({"name": name, "wins": 0, "losses": 0, "draws": 0, "specials_used": 0, "specials_successful": 0})
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(
            update.message.reply_text,
            f"🔥 <b>CONTRACT SIGNED!</b>\nYou are registered as <b>{name}</b>.\n"
            f"Add me to a group and start brawling with /startgame, /starttag, or /startrumble!",
            parse_mode=PARSE_MODE
        )
        return
    await safe_send(update.message.reply_text, "Commands: /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚡ <b>WWE CYBER-BRAWL — GAME MANUAL</b> ⚡\n\n"
        "<b>🏟️ Match Modes:</b>\n"
        "• /startgame — Open a 1v1 Grudge Match lobby\n"
        "• /starttag — Open a 2v2 Tag Team match (4 wrestlers)\n"
        "• /startrumble — Open a Royal Rumble lobby (4-8 wrestlers)\n"
        "• /forfeit — Forfeit your active match (DM)\n"
        "• /endmatch — Request a draw in group (participants only)\n\n"
        "<b>🥊 Combat System:</b>\n"
        "• Punch (10) | Kick (18) | Slam (26) | Dropkick (34)\n"
        "• <b>Suplex (48) & RKO (60):</b> 4 uses per match. Cannot be spammed consecutively.\n"
        "• <b>Reversal:</b> 3 uses per match. Reflects incoming damage back to the attacker!\n"
        "• <b>Tag Team:</b> Legal man can tag partner at any time. Partner enters automatically if legal man is pinned!\n\n"
        "<b>📊 Stats & Rankings:</b>\n"
        "• /stats — View your career card\n"
        "• /leaderboard — View Hall of Fame top 10\n"
        "• /checkstats @name — Inspect another superstar's record"
    )
    await safe_send(update.message.reply_text, msg, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in user_stats or not user_stats[uid].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start to register.")
        return
    info = user_stats[uid]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name", "Superstar"), info)
            bio = io.BytesIO(png)
            bio.name = "stats.png"
            bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            pass
    wins, losses, draws = info.get("wins", 0), info.get("losses", 0), info.get("draws", 0)
    total = wins + losses + draws
    pct = round((wins / total) * 100, 1) if total else 0.0
    txt = (
        f"🏆 <b>{info.get('name')}</b> — CAREER STATS\n"
        f"• Record: {wins}W - {losses}L - {draws}D\n"
        f"• Winrate: {pct}%\n"
        f"• Specials: {info.get('specials_successful', 0)} landed"
    )
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins", 0), info.get("losses", 0), info.get("draws", 0))
               for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered superstars yet.")
        return
    sorted_p = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_p)
            bio = io.BytesIO(png)
            bio.name = "leaderboard.png"
            bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            pass
    lines = ["🏆 <b>WWE HALL OF FAME:</b>\n"]
    for i, (n, w, l, d) in enumerate(sorted_p, start=1):
        lines.append(f"{i}. <b>{n}</b> — {w}W / {l}L / {d}D")
    await safe_send(update.message.reply_text, "\n".join(lines), parse_mode=PARSE_MODE)

async def cmd_checkstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").split()
    if len(text) < 2:
        await safe_send(update.message.reply_text, "Usage: /checkstats @username or WrestlerName")
        return
    target = text[1].lstrip("@").strip().lower()
    target_info = None
    for info in user_stats.values():
        if info.get("name") and info["name"].lower() == target:
            target_info = info
            break
    if not target_info:
        await safe_send(update.message.reply_text, f"No superstar found named '{text[1]}'.")
        return
    w, l, d = target_info.get("wins", 0), target_info.get("losses", 0), target_info.get("draws", 0)
    await safe_send(
        update.message.reply_text,
        f"🎖️ <b>{target_info.get('name')}</b>:\n{w} Wins | {l} Losses | {d} Draws",
        parse_mode=PARSE_MODE
    )

# ---------------- 1V1 MATCH ENGINE ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame inside a group.")
        return
    gid = update.effective_chat.id
    uid = update.effective_user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "Register first by sending /start in my private DM.")
        return
    if gid in games or gid in rumble_games or gid in tag_games:
        await safe_send(update.message.reply_text, "A brawl is already in progress in this arena!")
        return

    lobbies[gid] = {"host": uid, "players": [uid], "type": "1v1", "message_id": None}
    hname = user_stats[str(uid)]["name"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🥊 Step Into The Ring", callback_data=f"join|{gid}|{uid}")],
        [InlineKeyboardButton("❌ Cancel Match", callback_data=f"cancel_lobby|{gid}|{uid}")]
    ])
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=f"🎫 <b>WWE MAIN EVENT CHALLENGE!</b>\n<b>{hname}</b> is standing in the ring waiting for an opponent!\nTap below to accept!",
        parse_mode=PARSE_MODE,
        reply_markup=kb
    )
    if msg:
        lobbies[gid]["message_id"] = msg.message_id

async def lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    action, gid_s, hid_s = parts
    gid, hid, uid = int(gid_s), int(hid_s), query.from_user.id

    lobby = lobbies.get(gid)
    if not lobby or lobby.get("type") != "1v1":
        await query.answer("This match lobby has expired.", show_alert=True)
        return

    if action == "join":
        if uid == hid:
            await query.answer("You can't challenge yourself!", show_alert=True)
            return
        if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
            await query.answer("Register first! DM /start to the bot.", show_alert=True)
            return
        p1, p2 = hid, uid
        n1, n2 = user_stats[str(p1)]["name"], user_stats[str(p2)]["name"]
        games[gid] = {
            "players": [p1, p2],
            "names": {str(p1): n1, str(p2): n2},
            "hp": {p1: MAX_HP, p2: MAX_HP},
            "move_choice": {p1: None, p2: None},
            "last_move": {p1: None, p2: None},
            "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
            "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
            "round_msg_ids": []
        }
        del lobbies[gid]

        intro = (
            f"🔔 <b>DING! DING! DING!</b>\n"
            f"🔥 <b>{n1}</b> vs <b>{n2}</b>\n\n"
            f"{crowd_hype()}\n\n"
            f"Both gladiators are locked in! Choose your opening strike:"
        )
        await safe_send(context.bot.send_message, chat_id=gid, text=intro, parse_mode=PARSE_MODE)
        await send_1v1_move_prompt(gid, context)

    elif action == "cancel_lobby":
        if uid != hid:
            await query.answer("Only the challenger can cancel.", show_alert=True)
            return
        del lobbies[gid]
        await safe_send(context.bot.edit_message_text, chat_id=gid, message_id=lobby["message_id"], text="❌ Challenge cancelled.")

async def send_1v1_move_prompt(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(gid)
    if not game:
        return
    p1, p2 = game["players"]
    n1, n2 = game["names"][str(p1)], game["names"][str(p2)]
    hp1, hp2 = game["hp"][p1], game["hp"][p2]

    prompt = (
        f"⚡ <b>RING COMBAT</b>\n\n"
        f"🔴 <b>{n1}</b>\n{hp_bar(hp1)}\n\n"
        f"🔵 <b>{n2}</b>\n{hp_bar(hp2)}\n\n"
        f"<i>Both superstars, lock in your move:</i>"
    )
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=prompt,
        parse_mode=PARSE_MODE,
        reply_markup=build_1v1_move_keyboard(gid)
    )
    if msg:
        game["round_msg_ids"].append(msg.message_id)

async def move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    gid, move, uid = int(parts[1]), parts[2], query.from_user.id
    game = games.get(gid)
    if not game or uid not in game["players"]:
        await query.answer("You're not in this match!", show_alert=True)
        return

    last_move = game["last_move"].get(uid)
    if move in ["suplex", "rko"] and game["specials_left"][uid] <= 0:
        await query.answer("❌ No special moves left!", show_alert=True)
        return
    if move == "reversal" and game["reversals_left"][uid] <= 0:
        await query.answer("❌ No reversals left!", show_alert=True)
        return
    if move in ["suplex", "rko", "reversal"] and last_move == move:
        await query.answer("❌ Can't use the same special/reversal twice in a row!", show_alert=True)
        await send_short_restriction_dm(context, uid)
        return

    game["move_choice"][uid] = move
    game["last_move"][uid] = move
    choices = [v for v in game["move_choice"].values() if v is not None]
    if len(choices) == 2:
        await resolve_1v1_round(gid, context)
    else:
        await query.answer(f"✅ {MOVES[move]['name']} selected! Waiting for opponent...")

async def resolve_1v1_round(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = games[gid]
    p1, p2 = game["players"]
    m1, m2 = game["move_choice"][p1], game["move_choice"][p2]
    n1, n2 = game["names"][str(p1)], game["names"][str(p2)]
    d1, d2 = MOVES[m1]["dmg"], MOVES[m2]["dmg"]

    final_d1, final_d2 = d1, d2
    lines = []
    lines.append(f"💥 <b>{n1}</b> unleashes <b>{MOVES[m1]['name']}</b>!")
    lines.append(f"💥 <b>{n2}</b> strikes with <b>{MOVES[m2]['name']}</b>!\n")

    if m2 == "reversal" and m1 != "reversal":
        final_d1 = 0
        final_d2 = d1
        game["reversals_left"][p2] -= 1
        lines.append(f"🛡️ <b>{n2} REVERSES!</b> Catches {n1} off balance and smashes them for <b>{final_d2} DMG</b>!")
    elif m1 == "reversal" and m2 != "reversal":
        final_d1 = d2
        final_d2 = 0
        game["reversals_left"][p1] -= 1
        lines.append(f"🛡️ <b>{n1} REVERSES!</b> Counters {n2}'s assault for <b>{final_d1} DMG</b>!")
    elif m1 == "reversal" and m2 == "reversal":
        final_d1, final_d2 = 0, 0
        game["reversals_left"][p1] -= 1
        game["reversals_left"][p2] -= 1
        lines.append("🛡️ Both superstars attempt counters! They circle each other in a stalemate!")
    else:
        if final_d1 > 0:
            lines.append(f"⚡ <b>{n2}</b> takes <b>{final_d1} damage</b>!")
        if final_d2 > 0:
            lines.append(f"⚡ <b>{n1}</b> takes <b>{final_d2} damage</b>!")

    if m1 in ["suplex", "rko"]:
        game["specials_left"][p1] -= 1
        user_stats[str(p1)]["specials_used"] = user_stats[str(p1)].get("specials_used", 0) + 1
        if final_d1 > 0:
            user_stats[str(p1)]["specials_successful"] = user_stats[str(p1)].get("specials_successful", 0) + 1
    if m2 in ["suplex", "rko"]:
        game["specials_left"][p2] -= 1
        user_stats[str(p2)]["specials_used"] = user_stats[str(p2)].get("specials_used", 0) + 1
        if final_d2 > 0:
            user_stats[str(p2)]["specials_successful"] = user_stats[str(p2)].get("specials_successful", 0) + 1

    game["hp"][p1] -= final_d2
    game["hp"][p2] -= final_d1

    for mid in game.get("round_msg_ids", []):
        await safe_send(context.bot.edit_message_reply_markup, chat_id=gid, message_id=mid, reply_markup=None)
    game["round_msg_ids"] = []

    lines.append(f"\n{crowd_hype()}")
    await safe_send(context.bot.send_message, chat_id=gid, text="\n".join(lines), parse_mode=PARSE_MODE)

    hp1, hp2 = max(0, game["hp"][p1]), max(0, game["hp"][p2])
    if hp1 <= 0 or hp2 <= 0:
        await end_1v1_match(gid, context)
    else:
        game["move_choice"] = {p1: None, p2: None}
        await send_1v1_move_prompt(gid, context)

async def end_1v1_match(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = games[gid]
    p1, p2 = game["players"]
    n1, n2 = game["names"][str(p1)], game["names"][str(p2)]
    hp1, hp2 = max(0, game["hp"][p1]), max(0, game["hp"][p2])

    if hp1 <= 0 and hp2 <= 0:
        res = f"🤝 <b>DOUBLE KNOCKOUT — DRAW!</b>\nNeither <b>{n1}</b> nor <b>{n2}</b> could answer the 10 count!"
        user_stats[str(p1)]["draws"] = user_stats[str(p1)].get("draws", 0) + 1
        user_stats[str(p2)]["draws"] = user_stats[str(p2)].get("draws", 0) + 1
    elif hp1 <= 0:
        res = f"🏆 <b>1... 2... 3! PIN FALL!</b>\n🎉 <b>WINNER: {n2}!</b>\n💀 {n1} is laid out on the canvas!"
        user_stats[str(p1)]["losses"] = user_stats[str(p1)].get("losses", 0) + 1
        user_stats[str(p2)]["wins"] = user_stats[str(p2)].get("wins", 0) + 1
    else:
        res = f"🏆 <b>1... 2... 3! PIN FALL!</b>\n🎉 <b>WINNER: {n1}!</b>\n💀 {n2} is laid out on the canvas!"
        user_stats[str(p1)]["wins"] = user_stats[str(p1)].get("wins", 0) + 1
        user_stats[str(p2)]["losses"] = user_stats[str(p2)].get("losses", 0) + 1

    save_stats()
    del games[gid]
    await safe_send(context.bot.send_message, chat_id=gid, text=res, parse_mode=PARSE_MODE)

# ---------------- 2V2 TAG TEAM WARFARE ----------------
async def cmd_starttag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /starttag inside a group.")
        return
    gid = update.effective_chat.id
    uid = update.effective_user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "Register first by sending /start to my DM.")
        return
    if gid in games or gid in rumble_games or gid in tag_games:
        await safe_send(update.message.reply_text, "A brawl is already active here!")
        return

    lobbies[gid] = {
        "host": uid, "players": [uid], "type": "tag", "max": 4, "message_id": None
    }
    hname = user_stats[str(uid)]["name"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join Tag Team (1/4)", callback_data=f"join_tag|{gid}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_tag_lobby|{gid}|{uid}")]
    ])
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=f"🏟️ <b>2v2 TAG TEAM WARFARE OPENED!</b>\nHost: <b>{hname}</b>\nTap below to claim a corner! (4 superstars needed)",
        parse_mode=PARSE_MODE,
        reply_markup=kb
    )
    if msg:
        lobbies[gid]["message_id"] = msg.message_id

async def tag_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    action, gid, hid = parts[0], int(parts[1]), int(parts[2])
    uid = query.from_user.id

    lobby = lobbies.get(gid)
    if not lobby or lobby.get("type") != "tag":
        await query.answer("Lobby expired.", show_alert=True)
        return

    if action == "join_tag":
        if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
            await query.answer("Register first! DM /start to the bot.", show_alert=True)
            return
        if uid in lobby["players"]:
            await query.answer("You already joined!", show_alert=True)
            return
        lobby["players"].append(uid)
        count = len(lobby["players"])
        if count < 4:
            names = [user_stats[str(p)]["name"] for p in lobby["players"]]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔵 Join Tag Team ({count}/4)", callback_data=f"join_tag|{gid}|{hid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_tag_lobby|{gid}|{hid}")]
            ])
            await safe_send(
                context.bot.edit_message_text,
                chat_id=gid,
                message_id=lobby["message_id"],
                text=f"🏟️ <b>TAG TEAM LOBBY</b> ({count}/4)\n" + "\n".join([f"• {n}" for n in names]),
                parse_mode=PARSE_MODE,
                reply_markup=kb
            )
        else:
            p1, p2, p3, p4 = lobby["players"]
            del lobbies[gid]
            tag_games[gid] = {
                "team1": [p1, p2],
                "team2": [p3, p4],
                "active1": p1,
                "active2": p3,
                "hp": {p1: MAX_HP, p2: MAX_HP, p3: MAX_HP, p4: MAX_HP},
                "names": {str(p): user_stats[str(p)]["name"] for p in [p1, p2, p3, p4]},
                "move_choice": {},
                "last_move": {p: None for p in [p1, p2, p3, p4]},
                "specials": {p: MAX_SPECIALS_PER_MATCH for p in [p1, p2, p3, p4]},
                "reversals": {p: MAX_REVERSALS_PER_MATCH for p in [p1, p2, p3, p4]},
                "round_msg_ids": []
            }
            n = tag_games[gid]["names"]
            intro = (
                f"🔔 <b>TAG TEAM TORNADO STARTS!</b>\n\n"
                f"🔴 <b>TEAM RED:</b> {n[str(p1)]} & {n[str(p2)]}\n"
                f"🔵 <b>TEAM BLUE:</b> {n[str(p3)]} & {n[str(p4)]}\n\n"
                f"In the ring right now: <b>{n[str(p1)]}</b> vs <b>{n[str(p3)]}</b>!\n"
                f"{crowd_hype()}"
            )
            await safe_send(context.bot.send_message, chat_id=gid, text=intro, parse_mode=PARSE_MODE)
            await send_tag_move_prompt(gid, context)

    elif action == "cancel_tag_lobby":
        if uid != hid:
            await query.answer("Only the host can cancel.", show_alert=True)
            return
        del lobbies[gid]
        await safe_send(context.bot.edit_message_text, chat_id=gid, message_id=lobby["message_id"], text="❌ Tag team lobby cancelled.")

async def send_tag_move_prompt(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = tag_games.get(gid)
    if not game:
        return
    a1, a2 = game["active1"], game["active2"]
    n1, n2 = game["names"][str(a1)], game["names"][str(a2)]
    hp1, hp2 = game["hp"][a1], game["hp"][a2]

    partner1 = [p for p in game["team1"] if p != a1][0]
    partner2 = [p for p in game["team2"] if p != a2][0]

    prompt = (
        f"⚡ <b>TAG TEAM IN RING BATTLE</b>\n\n"
        f"🔴 <b>LEGAL: {n1}</b>\n{hp_bar(hp1)}\n"
        f"↳ <i>Apron: {game['names'][str(partner1)]} ({game['hp'][partner1]} HP)</i>\n\n"
        f"🔵 <b>LEGAL: {n2}</b>\n{hp_bar(hp2)}\n"
        f"↳ <i>Apron: {game['names'][str(partner2)]} ({game['hp'][partner2]} HP)</i>\n\n"
        f"<i>Active superstars, pick your move or tag your partner!</i>"
    )
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=prompt,
        parse_mode=PARSE_MODE,
        reply_markup=build_tag_move_keyboard(gid)
    )
    if msg:
        game["round_msg_ids"].append(msg.message_id)

async def tag_move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    gid, move, uid = int(parts[1]), parts[2], query.from_user.id
    game = tag_games.get(gid)
    if not game:
        return
    if uid not in [game["active1"], game["active2"]]:
        await query.answer("You are currently on the apron! Wait for a tag.", show_alert=True)
        return

    if move in ["suplex", "rko"] and game["specials"][uid] <= 0:
        await query.answer("No specials left!", show_alert=True)
        return
    if move == "reversal" and game["reversals"][uid] <= 0:
        await query.answer("No reversals left!", show_alert=True)
        return
    if move in ["suplex", "rko", "reversal"] and game["last_move"].get(uid) == move:
        await query.answer("Can't use the same special/reversal consecutively!", show_alert=True)
        await send_short_restriction_dm(context, uid)
        return

    game["move_choice"][uid] = move
    game["last_move"][uid] = move
    if len(game["move_choice"]) == 2:
        await resolve_tag_round(gid, context)
    else:
        await query.answer(f"Move locked: {move.upper()}!")

async def resolve_tag_round(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = tag_games[gid]
    a1, a2 = game["active1"], game["active2"]
    m1, m2 = game["move_choice"][a1], game["move_choice"][a2]
    n1, n2 = game["names"][str(a1)], game["names"][str(a2)]
    lines = []

    partner1 = [p for p in game["team1"] if p != a1][0]
    partner2 = [p for p in game["team2"] if p != a2][0]

    tagged1 = False
    tagged2 = False

    if m1 == "tag":
        if game["hp"][partner1] > 0:
            lines.append(f"🔄 <b>{n1} TAGS OUT!</b> <b>{game['names'][str(partner1)]}</b> sprints into the ring!")
            game["active1"] = partner1
            tagged1 = True
        else:
            lines.append(f"⚠️ {n1} tried to tag, but partner is already eliminated!")
    if m2 == "tag":
        if game["hp"][partner2] > 0:
            lines.append(f"🔄 <b>{n2} TAGS OUT!</b> <b>{game['names'][str(partner2)]}</b> enters the ring!")
            game["active2"] = partner2
            tagged2 = True
        else:
            lines.append(f"⚠️ {n2} tried to tag, but partner is already eliminated!")

    if not tagged1 and not tagged2:
        d1, d2 = MOVES[m1]["dmg"], MOVES[m2]["dmg"]
        if m2 == "reversal" and m1 != "reversal":
            game["hp"][a1] -= d1
            game["reversals"][a2] -= 1
            lines.append(f"🛡️ <b>{n2} reverses {n1}'s {m1}!</b> {n1} takes {d1} counter-damage!")
        elif m1 == "reversal" and m2 != "reversal":
            game["hp"][a2] -= d2
            game["reversals"][a1] -= 1
            lines.append(f"🛡️ <b>{n1} reverses {n2}'s {m2}!</b> {n2} takes {d2} counter-damage!")
        elif m1 == "reversal" and m2 == "reversal":
            lines.append("🛡️ Both attempt reversals! No damage dealt!")
        else:
            game["hp"][a2] -= d1
            game["hp"][a1] -= d2
            lines.append(f"💥 <b>{n1}</b> hits {m1.upper()} for {d1} DMG!")
            lines.append(f"💥 <b>{n2}</b> connects {m2.upper()} for {d2} DMG!")

    for mid in game.get("round_msg_ids", []):
        await safe_send(context.bot.edit_message_reply_markup, chat_id=gid, message_id=mid, reply_markup=None)
    game["round_msg_ids"] = []

    for t_active, t_partner, t_num in [(game["active1"], partner1, 1), (game["active2"], partner2, 2)]:
        if game["hp"][t_active] <= 0:
            lines.append(f"💀 <b>{game['names'][str(t_active)]} is ELIMINATED!</b>")
            if game["hp"][t_partner] > 0:
                lines.append(f"🚨 <b>{game['names'][str(t_partner)]}</b> enters the ring as the last hope!")
                if t_num == 1:
                    game["active1"] = t_partner
                else:
                    game["active2"] = t_partner

    lines.append(f"\n{crowd_hype()}")
    await safe_send(context.bot.send_message, chat_id=gid, text="\n".join(lines), parse_mode=PARSE_MODE)

    team1_dead = all(game["hp"][p] <= 0 for p in game["team1"])
    team2_dead = all(game["hp"][p] <= 0 for p in game["team2"])

    if team1_dead and team2_dead:
        await safe_send(context.bot.send_message, chat_id=gid, text="🤝 <b>TAG TEAM DRAW! Both teams eliminated!</b>", parse_mode=PARSE_MODE)
        del tag_games[gid]
    elif team1_dead:
        w1, w2 = game["names"][str(game["team2"][0])], game["names"][str(game["team2"][1])]
        await safe_send(context.bot.send_message, chat_id=gid, text=f"🏆 <b>TEAM BLUE WINS!</b>\nSuperstars <b>{w1}</b> & <b>{w2}</b> take home the belts!", parse_mode=PARSE_MODE)
        for p in game["team2"]:
            user_stats[str(p)]["wins"] = user_stats[str(p)].get("wins", 0) + 1
        for p in game["team1"]:
            user_stats[str(p)]["losses"] = user_stats[str(p)].get("losses", 0) + 1
        save_stats()
        del tag_games[gid]
    elif team2_dead:
        w1, w2 = game["names"][str(game["team1"][0])], game["names"][str(game["team1"][1])]
        await safe_send(context.bot.send_message, chat_id=gid, text=f"🏆 <b>TEAM RED WINS!</b>\nSuperstars <b>{w1}</b> & <b>{w2}</b> stand victorious!", parse_mode=PARSE_MODE)
        for p in game["team1"]:
            user_stats[str(p)]["wins"] = user_stats[str(p)].get("wins", 0) + 1
        for p in game["team2"]:
            user_stats[str(p)]["losses"] = user_stats[str(p)].get("losses", 0) + 1
        save_stats()
        del tag_games[gid]
    else:
        game["move_choice"] = {}
        await asyncio.sleep(2)
        await send_tag_move_prompt(gid, context)

# ---------------- ROYAL RUMBLE (4-8 PLAYERS) ----------------
async def cmd_startrumble(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startrumble inside a group.")
        return
    gid = update.effective_chat.id
    uid = update.effective_user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "Register first by sending /start in my private DM.")
        return
    if gid in games or gid in rumble_games or gid in tag_games:
        await safe_send(update.message.reply_text, "A brawl is already active in this arena.")
        return

    lobbies[gid] = {
        "host": uid, "players": [uid], "type": "rumble", "max": RUMBLE_MAX_PLAYERS, "message_id": None
    }
    hname = user_stats[str(uid)]["name"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Enter Royal Rumble (1/8)", callback_data=f"join_rumble|{gid}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rumble_lobby|{gid}|{uid}")]
    ])
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=f"👑 <b>ROYAL RUMBLE LOBBY OPENED!</b>\nHost: <b>{hname}</b>\nNeeded: 4 to 8 wrestlers. Tap below to draw your entry number!",
        parse_mode=PARSE_MODE,
        reply_markup=kb
    )
    if msg:
        lobbies[gid]["message_id"] = msg.message_id

async def rumble_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    action, gid, hid = parts[0], int(parts[1]), int(parts[2])
    uid = query.from_user.id

    lobby = lobbies.get(gid)
    if not lobby or lobby.get("type") != "rumble":
        await query.answer("Rumble lobby expired.", show_alert=True)
        return

    if action == "join_rumble":
        if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
            await query.answer("Register first! DM /start to the bot.", show_alert=True)
            return
        if uid in lobby["players"]:
            await query.answer("You're already in the rumble!", show_alert=True)
            return
        if len(lobby["players"]) >= RUMBLE_MAX_PLAYERS:
            await query.answer("Rumble is full!", show_alert=True)
            return
        lobby["players"].append(uid)
        count = len(lobby["players"])
        names = [user_stats[str(p)]["name"] for p in lobby["players"]]
        kb_rows = [[InlineKeyboardButton(f"👑 Enter Rumble ({count}/{RUMBLE_MAX_PLAYERS})", callback_data=f"join_rumble|{gid}|{hid}")]]
        if count >= RUMBLE_MIN_PLAYERS:
            kb_rows.insert(0, [InlineKeyboardButton("✅ START RUMBLE NOW!", callback_data=f"start_rumble|{gid}|{hid}")])
        kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rumble_lobby|{gid}|{hid}")])

        await safe_send(
            context.bot.edit_message_text,
            chat_id=gid,
            message_id=lobby["message_id"],
            text=f"👑 <b>ROYAL RUMBLE ENTRIES</b> ({count}/{RUMBLE_MAX_PLAYERS}):\n" + "\n".join([f"• #{i} {n}" for i, n in enumerate(names, 1)]),
            parse_mode=PARSE_MODE,
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )

    elif action == "start_rumble":
        if uid != hid:
            await query.answer("Only host can start!", show_alert=True)
            return
        wrestlers = lobby["players"].copy()
        del lobbies[gid]
        rumble_games[gid] = {
            "wrestlers": wrestlers,
            "active": wrestlers.copy(),
            "hp": {w: RUMBLE_HP for w in wrestlers},
            "names": {str(w): user_stats[str(w)]["name"] for w in wrestlers},
            "specials": {w: MAX_SPECIALS_PER_MATCH for w in wrestlers},
            "reversals": {w: MAX_REVERSALS_PER_MATCH for w in wrestlers},
            "last_move": {w: None for w in wrestlers},
            "choices": {},
            "round": 1,
            "round_msg_ids": []
        }
        await safe_send(
            context.bot.send_message,
            chat_id=gid,
            text=f"🚨 <b>THE BUZZER SOUNDS! 3... 2... 1!</b>\n👑 <b>ROYAL RUMBLE IS UNDERWAY!</b>\nLast superstar inside the ring wins!",
            parse_mode=PARSE_MODE
        )
        await start_rumble_round(gid, context)

    elif action == "cancel_rumble_lobby":
        if uid != hid:
            await query.answer("Only host can cancel.", show_alert=True)
            return
        del lobbies[gid]
        await safe_send(context.bot.edit_message_text, chat_id=gid, message_id=lobby["message_id"], text="❌ Rumble cancelled.")

async def start_rumble_round(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = rumble_games.get(gid)
    if not game:
        return
    if len(game["active"]) <= 1:
        await end_royal_rumble(gid, context)
        return

    game["choices"] = {}
    status = [f"🤼 <b>{game['names'][str(w)]}</b>\n{hp_bar(game['hp'][w], RUMBLE_HP)}" for w in game["active"]]
    txt = (
        f"⚡ <b>ROYAL RUMBLE — ROUND {game['round']}</b>\n\n" +
        "\n\n".join(status) +
        f"\n\n🥊 <i>All active superstars, select your move:</i>"
    )
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=txt,
        parse_mode=PARSE_MODE,
        reply_markup=build_rumble_move_keyboard(gid)
    )
    if msg:
        game["round_msg_ids"].append(msg.message_id)

async def rumble_move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    gid, move, uid = int(parts[1]), parts[2], query.from_user.id
    game = rumble_games.get(gid)
    if not game or uid not in game["active"]:
        await query.answer("You are eliminated or not in this rumble!", show_alert=True)
        return

    if move in ["suplex", "rko"] and game["specials"][uid] <= 0:
        await query.answer("No specials left!", show_alert=True)
        return
    if move == "reversal" and game["reversals"][uid] <= 0:
        await query.answer("No reversals left!", show_alert=True)
        return
    if move in ["suplex", "rko", "reversal"] and game["last_move"].get(uid) == move:
        await query.answer("Can't use the same special/reversal consecutively!", show_alert=True)
        await send_short_restriction_dm(context, uid)
        return

    if move == "reversal":
        game["choices"][uid] = {"move": "reversal", "target": None}
        game["last_move"][uid] = "reversal"
        await query.answer("🛡️ Reversal ready! Waiting for others...")
        if len(game["choices"]) == len(game["active"]):
            await resolve_rumble_round(gid, context)
        return

    opponents = [w for w in game["active"] if w != uid]
    cid = f"{uid}_{move}_{random.randint(100, 999)}"
    rumble_target_choices[cid] = {"uid": uid, "gid": gid, "move": move, "opponents": opponents}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(game["names"][str(op)], callback_data=f"rumble_target|{cid}|{op}")]
        for op in opponents
    ])
    dm_sent = await safe_send(
        context.bot.send_message,
        chat_id=uid,
        text=f"🎯 <b>Pick your target for {MOVES[move]['name']}!</b>",
        parse_mode=PARSE_MODE,
        reply_markup=kb
    )
    if dm_sent:
        await query.answer(f"Check your DM to select target for {move.upper()}!")
    else:
        await query.answer("⚠️ Open DM with me first (send /start in DM) so I can send the target menu!", show_alert=True)

async def rumble_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    cid, target_id = parts[1], int(parts[2])
    data = rumble_target_choices.get(cid)
    if not data:
        await query.answer("Target choice expired.", show_alert=True)
        return

    uid, gid, move = data["uid"], data["gid"], data["move"]
    if query.from_user.id != uid:
        return
    game = rumble_games.get(gid)
    if not game or uid not in game["active"]:
        return

    game["choices"][uid] = {"move": move, "target": target_id}
    game["last_move"][uid] = move
    del rumble_target_choices[cid]

    tname = game["names"][str(target_id)]
    await safe_send(
        context.bot.edit_message_text,
        chat_id=uid,
        message_id=query.message.message_id,
        text=f"✅ Target locked: <b>{tname}</b> with <b>{MOVES[move]['name']}</b>!",
        parse_mode=PARSE_MODE
    )

    if len(game["choices"]) == len(game["active"]):
        await resolve_rumble_round(gid, context)

async def resolve_rumble_round(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = rumble_games[gid]
    active = game["active"]
    lines = [f"⚡ <b>RUMBLE ROUND {game['round']} RESOLUTION:</b>\n"]

    damage_taken = {w: 0 for w in active}

    # Consume 1 reversal only for players using reversal this round
    for w in active:
        if game["choices"].get(w, {}).get("move") == "reversal":
            game["reversals"][w] -= 1

    for attacker in active:
        c = game["choices"].get(attacker)
        if not c or c["move"] == "reversal":
            continue
        mv = c["move"]
        tgt = c["target"]
        dmg = MOVES[mv]["dmg"]
        aname = game["names"][str(attacker)]
        tname = game["names"][str(tgt)]

        if mv in ["suplex", "rko"]:
            game["specials"][attacker] -= 1
            user_stats[str(attacker)]["specials_used"] = user_stats[str(attacker)].get("specials_used", 0) + 1

        if game["choices"].get(tgt, {}).get("move") == "reversal":
            damage_taken[attacker] += dmg
            lines.append(f"🛡️ <b>{tname} REVERSES {aname}'s {mv.upper()}!</b> {aname} takes {dmg} DMG!")
        else:
            damage_taken[tgt] += dmg
            lines.append(f"💥 <b>{aname}</b> strikes <b>{tname}</b> with {mv.upper()} for {dmg} DMG!")
            if mv in ["suplex", "rko"]:
                user_stats[str(attacker)]["specials_successful"] = user_stats[str(attacker)].get("specials_successful", 0) + 1

    eliminated = []
    lines.append("")
    for w in active:
        game["hp"][w] -= damage_taken[w]
        wname = game["names"][str(w)]
        if game["hp"][w] <= 0:
            eliminated.append(w)
            lines.append(f"💀 <b>{wname} WAS THROWN OVER THE TOP ROPE! ELIMINATED!</b>")
        else:
            lines.append(f"❤️ {wname}: {game['hp'][w]} HP remaining")

    for e in eliminated:
        game["active"].remove(e)

    for mid in game.get("round_msg_ids", []):
        await safe_send(context.bot.edit_message_reply_markup, chat_id=gid, message_id=mid, reply_markup=None)
    game["round_msg_ids"] = []

    lines.append(f"\n{crowd_hype()}")
    await safe_send(context.bot.send_message, chat_id=gid, text="\n".join(lines), parse_mode=PARSE_MODE)

    if len(game["active"]) <= 1:
        await end_royal_rumble(gid, context)
    else:
        game["round"] += 1
        await asyncio.sleep(2)
        await start_rumble_round(gid, context)

async def end_royal_rumble(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = rumble_games[gid]
    if len(game["active"]) == 1:
        winner = game["active"][0]
        wname = game["names"][str(winner)]
        txt = (
            f"👑 <b>ROYAL RUMBLE WINNER!</b>\n\n"
            f"🎉 <b>{wname}</b> is the LAST SUPERSTAR STANDING!\n"
            f"Guaranteed headline spot at WrestleMania!\n\n"
            f"{crowd_hype()}"
        )
        user_stats[str(winner)]["wins"] = user_stats[str(winner)].get("wins", 0) + 1
        for w in game["wrestlers"]:
            if w != winner:
                user_stats[str(w)]["losses"] = user_stats[str(w)].get("losses", 0) + 1
    else:
        txt = "🤝 <b>ROYAL RUMBLE DRAW! All remaining superstars were eliminated!</b>"
        for w in game["wrestlers"]:
            user_stats[str(w)]["draws"] = user_stats[str(w)].get("draws", 0) + 1

    save_stats()
    del rumble_games[gid]
    await safe_send(context.bot.send_message, chat_id=gid, text=txt, parse_mode=PARSE_MODE)

# ---------------- FORFEIT & ENDMATCH ----------------
async def cmd_forfeit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Use /forfeit in my private DM.")
        return
    uid = update.effective_user.id
    target_gid = None
    for gid, g in games.items():
        if uid in g["players"]:
            target_gid = gid
            break
    if not target_gid:
        await safe_send(update.message.reply_text, "You are not in any active 1v1 match.")
        return
    g = games[target_gid]
    p1, p2 = g["players"]
    winner = p2 if uid == p1 else p1
    wname = g["names"][str(winner)]
    lname = g["names"][str(uid)]
    user_stats[str(winner)]["wins"] = user_stats[str(winner)].get("wins", 0) + 1
    user_stats[str(uid)]["losses"] = user_stats[str(uid)].get("losses", 0) + 1
    save_stats()
    del games[target_gid]
    await safe_send(update.message.reply_text, "You tapped out and forfeited the match.")
    await safe_send(context.bot.send_message, chat_id=target_gid, text=f"⏹️ <b>{lname} threw in the towel!</b>\n🏆 <b>WINNER BY FORFEIT: {wname}!</b>", parse_mode=PARSE_MODE)

async def cmd_endmatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    uid = update.effective_user.id
    if gid not in games:
        await safe_send(update.message.reply_text, "No active match here.")
        return
    if uid not in games[gid]["players"]:
        await safe_send(update.message.reply_text, "Only combatants can call for a stoppage.")
        return
    del games[gid]
    await safe_send(update.message.reply_text, "⏹️ <b>Match ended by mutual referee stoppage!</b>", parse_mode=PARSE_MODE)

# ---------------- LIGHTWEIGHT WEB SERVER FOR 24/7 HOSTING ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("HOMIES WWE BOT IS RUNNING 24/7 🔥".encode("utf-8"))

    def log_message(self, format, *args):
        return  # Suppress console log spam

def run_keepalive_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), PingHandler)
        logger.info("Web health server listening on port %s", PORT)
        server.serve_forever()
    except Exception as e:
        logger.error("Health server error: %s", e)

# ---------------- BOT INITIALIZATION ----------------
def main():
    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN found.")
        return

    # Build Telegram Bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("startcareer", cmd_startcareer))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("checkstats", cmd_checkstats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("starttag", cmd_starttag))
    app.add_handler(CommandHandler("startrumble", cmd_startrumble))
    app.add_handler(CommandHandler("forfeit", cmd_forfeit))
    app.add_handler(CommandHandler("endmatch", cmd_endmatch))

    # Callbacks
    app.add_handler(CallbackQueryHandler(lobby_callback, pattern=r"^(join|cancel_lobby)\|"))
    app.add_handler(CallbackQueryHandler(move_callback, pattern=r"^move\|"))
    app.add_handler(CallbackQueryHandler(tag_lobby_callback, pattern=r"^(join_tag|cancel_tag_lobby)\|"))
    app.add_handler(CallbackQueryHandler(tag_move_callback, pattern=r"^tag_move\|"))
    app.add_handler(CallbackQueryHandler(rumble_lobby_callback, pattern=r"^(join_rumble|cancel_rumble_lobby|start_rumble)\|"))
    app.add_handler(CallbackQueryHandler(rumble_move_callback, pattern=r"^rumble_move\|"))
    app.add_handler(CallbackQueryHandler(rumble_target_callback, pattern=r"^rumble_target\|"))

    # Private text handler
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, private_text_handler))

    # Launch background keepalive HTTP ping server in a separate thread
    server_thread = threading.Thread(target=run_keepalive_server, daemon=True)
    server_thread.start()

    logger.info("⚡ WWE CYBER ARENA BOT IS LIVE!")
    app.run_polling()

if __name__ == "__main__":
    main()
