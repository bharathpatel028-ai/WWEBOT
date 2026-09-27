#!/usr/bin/env python3
"""
🏆 HOMIES WWE CYBER-BRAWL BOT (24/7 CLOUD EDITION)
- Clean, spacious visual layout with neon emojis & dividers
- Auto-registers players instantly (never blocks with 'DM /start first')
- Preserves all stats across updates
- 1v1 Grudge, 2v2 Tag Team, & 4-8 Player Royal Rumble
- Built-in 24/7 Keepalive HTTP Server for Render
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

# ---------------- CONFIG & SECRETS ----------------
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

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"

MOVES = {
    "punch": {"dmg": 10, "emoji": "🥊", "name": "Heavy Punch"},
    "kick": {"dmg": 18, "emoji": "🦵", "name": "Big Boot"},
    "slam": {"dmg": 26, "emoji": "💥", "name": "Body Slam"},
    "dropkick": {"dmg": 34, "emoji": "🚀", "name": "Dropkick"},
    "suplex": {"dmg": 48, "emoji": "🌪️", "name": "German Suplex"},
    "rko": {"dmg": 60, "emoji": "⚡", "name": "Outta Nowhere RKO"},
    "reversal": {"dmg": 0, "emoji": "🛡️", "name": "Counter Reversal"},
}

# ---------------- PERMANENT STATS STORAGE ----------------
try:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            user_stats: Dict[str, Dict] = json.load(f)
    else:
        user_stats = {}
except Exception:
    logger.exception("Failed to load stats; starting clean.")
    user_stats = {}

def save_stats():
    try:
        parent = os.path.dirname(STATS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_stats, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save stats.")

for k, v in list(user_stats.items()):
    v.setdefault("draws", 0)

def ensure_user(user) -> str:
    """Auto-registers any player instantly so no one is blocked from playing."""
    uid = str(user.id)
    if uid not in user_stats or not user_stats[uid].get("name"):
        fallback = user.first_name or user.username or f"Superstar_{user.id}"
        clean_name = re.sub(r'[^\w\s-]', '', fallback).strip()[:MAX_NAME_LENGTH]
        if not clean_name:
            clean_name = f"Wrestler_{str(user.id)[-4:]}"
        user_stats.setdefault(uid, {})
        user_stats[uid].update({
            "name": clean_name,
            "wins": user_stats[uid].get("wins", 0),
            "losses": user_stats[uid].get("losses", 0),
            "draws": user_stats[uid].get("draws", 0),
            "specials_used": user_stats[uid].get("specials_used", 0),
            "specials_successful": user_stats[uid].get("specials_successful", 0),
        })
        save_stats()
    return user_stats[uid]["name"]

# ---------------- IN-MEMORY ACTIVE MATCHES ----------------
lobbies: Dict[int, Dict] = {}
games: Dict[int, Dict] = {}
rumble_games: Dict[int, Dict] = {}
tag_games: Dict[int, Dict] = {}
rumble_target_choices: Dict[str, Dict] = {}

# ---------------- VISUAL RETRO HP BAR ----------------
def hp_bar(current: int, max_hp: int = MAX_HP, length: int = 10) -> str:
    current = max(0, current)
    ratio = current / max_hp
    filled = int(round(ratio * length))
    empty = max(0, length - filled)
    if ratio > 0.50:
        bar_char = "🟩"
    elif ratio > 0.20:
        bar_char = "🟨"
    else:
        bar_char = "🟥"
    return f"{bar_char * filled}{'⬛' * empty}  <b>{current} / {max_hp} HP</b>"

def crowd_hype() -> str:
    return random.choice([
        "🔥 <i>The arena roof blows off with thunderous cheers!</i>",
        "📣 <i>Fans are on their feet, screaming at ringside!</i>",
        "😱 <i>HOLY SHIT! echoes throughout the packed arena!</i>",
        "⚡ <i>Camera flashes light up the square circle!</i>",
        "🎙️ <i>ANNOUNCER: 'BAH GAWD! HE HIT HIM WITH EVERYTHING!'</i>"
    ])

async def safe_send(func, *args, **kwargs):
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

# ---------------- HIGH-DEF TRADING CARD GENERATOR ----------------
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
                return (ImageFont.truetype(p, 48), ImageFont.truetype(p, 24))
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
    W, H = 840, 420
    img = Image.new("RGB", (W, H), color=(14, 17, 28))
    draw = ImageDraw.Draw(img)

    # Gold and crimson frame
    draw.rectangle(((0, 0), (W, 85)), fill=(200, 16, 46))
    draw.line(((0, 85), (W, 85)), fill=(255, 215, 0), width=4)
    draw.text((30, 20), "🏆 WWE CHAMPIONSHIP ROSTER", font=title_font, fill=(255, 255, 255))

    # Name
    draw.text((40, 105), f"⭐ SUPERSTAR: {name.upper()}", font=title_font, fill=(255, 215, 0))

    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    draws = stats.get("draws", 0)
    total = wins + losses + draws
    win_pct = round((wins / total) * 100, 1) if total else 0.0
    sp_u = stats.get("specials_used", 0)
    sp_s = stats.get("specials_successful", 0)

    # Stat cards
    y = 180
    draw.rectangle(((40, y), (260, y + 90)), fill=(22, 28, 48), outline=(0, 255, 180), width=2)
    draw.text((60, y + 15), "VICTORIES", font=body_font, fill=(160, 180, 210))
    draw.text((60, y + 45), f"{wins} WINS", font=title_font, fill=(0, 255, 180))

    draw.rectangle(((300, y), (520, y + 90)), fill=(22, 28, 48), outline=(255, 75, 75), width=2)
    draw.text((320, y + 15), "DEFEATS", font=body_font, fill=(160, 180, 210))
    draw.text((320, y + 45), f"{losses} LOSS", font=title_font, fill=(255, 80, 80))

    draw.rectangle(((560, y), (780, y + 90)), fill=(22, 28, 48), outline=(255, 200, 0), width=2)
    draw.text((580, y + 15), "WIN RATE", font=body_font, fill=(160, 180, 210))
    draw.text((580, y + 45), f"{win_pct}%", font=title_font, fill=(255, 215, 0))

    footer = f"⚡ Specials Landed: {sp_s} / {sp_u}  •  Draws: {draws}"
    draw.text((45, 300), footer, font=body_font, fill=(200, 210, 230))
    draw.text((250, H - 40), "★ WWE CYBER ARENA 24/7 ★", font=body_font, fill=(100, 115, 145))

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
    H = 130 + rows * 48
    img = Image.new("RGB", (W, H), color=(12, 16, 26))
    draw = ImageDraw.Draw(img)
    draw.rectangle(((0, 0), (W, 85)), fill=(25, 118, 210))
    draw.line(((0, 85), (W, 85)), fill=(0, 229, 255), width=4)
    draw.text((30, 20), "👑 WWE HALL OF FAME — TOP 10", font=title_font, fill=(255, 255, 255))
    y = 110
    for i, (name, wins, losses, draws) in enumerate(entries, start=1):
        medal = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else f"{i}. "
        draw.text((40, y), f"{medal}{name}", font=body_font, fill=(255, 255, 255))
        draw.text((W - 320, y), f"{wins}W - {losses}L - {draws}D", font=body_font, fill=(0, 255, 200))
        y += 48
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

# ---------------- KEYBOARDS ----------------
def build_1v1_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 Punch • 10", callback_data=f"move|{group_id}|punch"),
            InlineKeyboardButton("🦵 Kick • 18", callback_data=f"move|{group_id}|kick"),
            InlineKeyboardButton("💥 Slam • 26", callback_data=f"move|{group_id}|slam")
        ],
        [
            InlineKeyboardButton("🚀 Dropkick • 34", callback_data=f"move|{group_id}|dropkick"),
            InlineKeyboardButton("🌪️ Suplex • 48", callback_data=f"move|{group_id}|suplex"),
            InlineKeyboardButton("⚡ RKO • 60", callback_data=f"move|{group_id}|rko")
        ],
        [
            InlineKeyboardButton("🛡️ Counter / Reversal", callback_data=f"move|{group_id}|reversal")
        ]
    ])

def build_rumble_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 Punch • 10", callback_data=f"rumble_move|{group_id}|punch"),
            InlineKeyboardButton("🦵 Kick • 18", callback_data=f"rumble_move|{group_id}|kick"),
            InlineKeyboardButton("💥 Slam • 26", callback_data=f"rumble_move|{group_id}|slam")
        ],
        [
            InlineKeyboardButton("🚀 Dropkick • 34", callback_data=f"rumble_move|{group_id}|dropkick"),
            InlineKeyboardButton("🌪️ Suplex • 48", callback_data=f"rumble_move|{group_id}|suplex"),
            InlineKeyboardButton("⚡ RKO • 60", callback_data=f"rumble_move|{group_id}|rko")
        ],
        [
            InlineKeyboardButton("🛡️ Reversal", callback_data=f"rumble_move|{group_id}|reversal")
        ]
    ])

def build_tag_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥊 Punch • 10", callback_data=f"tag_move|{group_id}|punch"),
            InlineKeyboardButton("🦵 Kick • 18", callback_data=f"tag_move|{group_id}|kick"),
            InlineKeyboardButton("💥 Slam • 26", callback_data=f"tag_move|{group_id}|slam")
        ],
        [
            InlineKeyboardButton("🚀 Dropkick • 34", callback_data=f"tag_move|{group_id}|dropkick"),
            InlineKeyboardButton("🌪️ Suplex • 48", callback_data=f"tag_move|{group_id}|suplex"),
            InlineKeyboardButton("⚡ RKO • 60", callback_data=f"tag_move|{group_id}|rko")
        ],
        [
            InlineKeyboardButton("🔄 Tag Partner", callback_data=f"tag_move|{group_id}|tag"),
            InlineKeyboardButton("🛡️ Reversal", callback_data=f"tag_move|{group_id}|reversal")
        ]
    ])

# ---------------- USER COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = ensure_user(update.effective_user)
    msg = (
        f"⚡ <b>WELCOME TO WWE CYBER-BRAWL!</b> ⚡\n"
        f"{DIVIDER}\n\n"
        f"🥊 <b>Your Ring Name:</b> <code>{name}</code>\n"
        f"🏆 <b>Status:</b> Ready for Combat\n\n"
        f"<b>Commands you can use:</b>\n"
        f"• /startgame — Open 1v1 Grudge Match (in group)\n"
        f"• /starttag — Open 2v2 Tag Team match (in group)\n"
        f"• /startrumble — Open Royal Rumble 4-8 players (in group)\n"
        f"• /stats — View your career card\n"
        f"• /leaderboard — View Hall of Fame top 10\n"
        f"• /startcareer &lt;name&gt; — Change your ring name\n\n"
        f"{DIVIDER}"
    )
    await safe_send(update.message.reply_text, msg, parse_mode=PARSE_MODE)

async def cmd_startcareer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    uid = str(update.effective_user.id)
    ensure_user(update.effective_user)
    if not args:
        await safe_send(update.message.reply_text, "Usage: <code>/startcareer &lt;NewName&gt;</code>", parse_mode=PARSE_MODE)
        return
    new_name = " ".join(args).strip()[:MAX_NAME_LENGTH]
    user_stats[uid]["name"] = new_name
    save_stats()
    await safe_send(update.message.reply_text, f"🔥 <b>Ring name updated to:</b> <code>{new_name}</code>!", parse_mode=PARSE_MODE)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📖 <b>WWE BRAWLER — RULEBOOK</b>\n"
        f"{DIVIDER}\n\n"
        f"<b>🏟️ Game Modes:</b>\n"
        f"• <b>/startgame</b> — 1v1 Grudge match\n"
        f"• <b>/starttag</b> — 2v2 Tag Team brawl\n"
        f"• <b>/startrumble</b> — 4 to 8 Superstars Royal Rumble\n\n"
        f"<b>🥊 Move List & Damage:</b>\n"
        f"• 🥊 Punch [10]  • 🦵 Kick [18]  • 💥 Slam [26]\n"
        f"• 🚀 Dropkick [34]  • 🌪️ Suplex [48]  • ⚡ RKO [60]\n"
        f"• 🛡️ <b>Reversal:</b> Reflects 100% of damage back to the attacker!\n\n"
        f"<b>⚠️ Restrictions:</b>\n"
        f"• Specials (Suplex & RKO) max 4 per match.\n"
        f"• Reversals max 3 per match.\n"
        f"• You cannot spam the same special or reversal twice in a row.\n\n"
        f"{DIVIDER}"
    )
    await safe_send(update.message.reply_text, msg, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ensure_user(update.effective_user)
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
    w, l, d = info.get("wins", 0), info.get("losses", 0), info.get("draws", 0)
    msg = (
        f"🏆 <b>CAREER PROFILE: {info.get('name')}</b>\n"
        f"{DIVIDER}\n"
        f"🥇 <b>Victories:</b> {w}\n"
        f"💀 <b>Defeats:</b> {l}\n"
        f"🤝 <b>Draws:</b> {d}\n"
        f"⚡ <b>Specials Landed:</b> {info.get('specials_successful', 0)}\n"
        f"{DIVIDER}"
    )
    await safe_send(update.message.reply_text, msg, parse_mode=PARSE_MODE)

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
    lines = [f"👑 <b>WWE HALL OF FAME — TOP 10</b>\n{DIVIDER}\n"]
    for i, (n, w, l, d) in enumerate(sorted_p, start=1):
        lines.append(f"{i}. <b>{n}</b> — {w}W / {l}L / {d}D")
    lines.append(f"\n{DIVIDER}")
    await safe_send(update.message.reply_text, "\n".join(lines), parse_mode=PARSE_MODE)

# ---------------- 1V1 GRUDGE MATCH ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame inside a group to challenge others!")
        return
    gid = update.effective_chat.id
    user = update.effective_user
    uid = user.id
    hname = ensure_user(user)

    if gid in games or gid in rumble_games or gid in tag_games:
        await safe_send(update.message.reply_text, "⚠️ A brawl is already active in this arena! Finish it first.")
        return

    lobbies[gid] = {"host": uid, "players": [uid], "type": "1v1", "message_id": None}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🥊 Step Into The Ring", callback_data=f"join|{gid}|{uid}")],
        [InlineKeyboardButton("❌ Cancel Match", callback_data=f"cancel_lobby|{gid}|{uid}")]
    ])
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=(
            f"🎫 <b>WWE MAIN EVENT CHALLENGE!</b>\n"
            f"{DIVIDER}\n\n"
            f"🔥 Superstar <b>{hname}</b> is standing in the ring!\n"
            f"Who has the guts to accept the challenge?\n\n"
            f"{DIVIDER}"
        ),
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
    action, gid, hid = parts[0], int(parts[1]), int(parts[2])
    user = query.from_user
    uid = user.id

    lobby = lobbies.get(gid)
    if not lobby or lobby.get("type") != "1v1":
        await query.answer("This match lobby has expired.", show_alert=True)
        return

    if action == "join":
        if uid == hid:
            await query.answer("You can't fight yourself! Wait for a challenger.", show_alert=True)
            return
        p1, p2 = hid, uid
        n1 = user_stats[str(p1)]["name"]
        n2 = ensure_user(user)

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
            f"{DIVIDER}\n\n"
            f"🥊 <b>{n1}</b> vs <b>{n2}</b>\n\n"
            f"{crowd_hype()}\n\n"
            f"{DIVIDER}"
        )
        await safe_send(context.bot.send_message, chat_id=gid, text=intro, parse_mode=PARSE_MODE)
        await send_1v1_move_prompt(gid, context)

    elif action == "cancel_lobby":
        if uid != hid:
            await query.answer("Only the host can cancel.", show_alert=True)
            return
        del lobbies[gid]
        await safe_send(context.bot.edit_message_text, chat_id=gid, message_id=lobby["message_id"], text="❌ Match challenge cancelled.")

async def send_1v1_move_prompt(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(gid)
    if not game:
        return
    p1, p2 = game["players"]
    n1, n2 = game["names"][str(p1)], game["names"][str(p2)]
    hp1, hp2 = game["hp"][p1], game["hp"][p2]

    prompt = (
        f"⚡ <b>RINGSIDE ACTION</b>\n"
        f"{DIVIDER}\n\n"
        f"🔴 <b>{n1}</b>\n{hp_bar(hp1)}\n\n"
        f"🔵 <b>{n2}</b>\n{hp_bar(hp2)}\n\n"
        f"{DIVIDER}\n"
        f"<i>Both superstars, tap your move below:</i>"
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
        await query.answer("You're not a competitor in this match!", show_alert=True)
        return

    last_move = game["last_move"].get(uid)
    if move in ["suplex", "rko"] and game["specials_left"][uid] <= 0:
        await query.answer("❌ No special moves left!", show_alert=True)
        return
    if move == "reversal" and game["reversals_left"][uid] <= 0:
        await query.answer("❌ No reversals left!", show_alert=True)
        return
    if move in ["suplex", "rko", "reversal"] and last_move == move:
        await query.answer("❌ Can't use the same special/reversal consecutively!", show_alert=True)
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
    lines = [
        f"💥 <b>{n1}</b> strikes with <b>{MOVES[m1]['name']}</b>!",
        f"💥 <b>{n2}</b> retaliates with <b>{MOVES[m2]['name']}</b>!\n"
    ]

    if m2 == "reversal" and m1 != "reversal":
        final_d1 = 0
        final_d2 = d1
        game["reversals_left"][p2] -= 1
        lines.append(f"🛡️ <b>{n2} COUNTERS!</b> Flips {n1} overhead for <b>{final_d2} REFLECTED DMG</b>!")
    elif m1 == "reversal" and m2 != "reversal":
        final_d1 = d2
        final_d2 = 0
        game["reversals_left"][p1] -= 1
        lines.append(f"🛡️ <b>{n1} COUNTERS!</b> Turns {n2}'s momentum into <b>{final_d1} REFLECTED DMG</b>!")
    elif m1 == "reversal" and m2 == "reversal":
        final_d1, final_d2 = 0, 0
        game["reversals_left"][p1] -= 1
        game["reversals_left"][p2] -= 1
        lines.append("🛡️ Both attempt counters! They bounce off the ropes in a stalemate!")
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

    lines.append(f"\n{DIVIDER}")
    lines.append(f"{crowd_hype()}")
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
        res = f"🤝 <b>DOUBLE KNOCKOUT — DRAW!</b>\nNeither <b>{n1}</b> nor <b>{n2}</b> could answer the 10-count!"
        user_stats[str(p1)]["draws"] = user_stats[str(p1)].get("draws", 0) + 1
        user_stats[str(p2)]["draws"] = user_stats[str(p2)].get("draws", 0) + 1
    elif hp1 <= 0:
        res = f"🏆 <b>1... 2... 3! PIN FALL!</b>\n🎉 <b>WINNER: {n2}!</b>\n💀 {n1} is laid out flat on the canvas!"
        user_stats[str(p1)]["losses"] = user_stats[str(p1)].get("losses", 0) + 1
        user_stats[str(p2)]["wins"] = user_stats[str(p2)].get("wins", 0) + 1
    else:
        res = f"🏆 <b>1... 2... 3! PIN FALL!</b>\n🎉 <b>WINNER: {n1}!</b>\n💀 {n2} is laid out flat on the canvas!"
        user_stats[str(p1)]["wins"] = user_stats[str(p1)].get("wins", 0) + 1
        user_stats[str(p2)]["losses"] = user_stats[str(p2)].get("losses", 0) + 1

    save_stats()
    del games[gid]
    await safe_send(context.bot.send_message, chat_id=gid, text=f"{DIVIDER}\n{res}\n{DIVIDER}", parse_mode=PARSE_MODE)

# ---------------- 2V2 TAG TEAM ----------------
async def cmd_starttag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /starttag inside a group.")
        return
    gid = update.effective_chat.id
    user = update.effective_user
    uid = user.id
    hname = ensure_user(user)

    if gid in games or gid in rumble_games or gid in tag_games:
        await safe_send(update.message.reply_text, "A brawl is already active in this arena!")
        return

    lobbies[gid] = {"host": uid, "players": [uid], "type": "tag", "max": 4, "message_id": None}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join Tag Team (1/4)", callback_data=f"join_tag|{gid}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_tag_lobby|{gid}|{uid}")]
    ])
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=(
            f"🏟️ <b>2v2 TAG TEAM WARFARE OPENED!</b>\n"
            f"{DIVIDER}\n\n"
            f"Host: <b>{hname}</b>\n"
            f"Need 4 superstars. Tap below to claim a corner!\n\n"
            f"{DIVIDER}"
        ),
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
    user = query.from_user
    uid = user.id

    lobby = lobbies.get(gid)
    if not lobby or lobby.get("type") != "tag":
        await query.answer("Lobby expired.", show_alert=True)
        return

    if action == "join_tag":
        ensure_user(user)
        if uid in lobby["players"]:
            await query.answer("You are already in this lobby!", show_alert=True)
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
                text=f"🏟️ <b>TAG TEAM LOBBY</b> ({count}/4)\n{DIVIDER}\n" + "\n".join([f"• <b>{n}</b>" for n in names]) + f"\n{DIVIDER}",
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
                f"🔔 <b>TAG TEAM TORNADO STARTS!</b>\n"
                f"{DIVIDER}\n\n"
                f"🔴 <b>TEAM RED:</b> {n[str(p1)]} & {n[str(p2)]}\n"
                f"🔵 <b>TEAM BLUE:</b> {n[str(p3)]} & {n[str(p4)]}\n\n"
                f"Legal men in ring: <b>{n[str(p1)]}</b> vs <b>{n[str(p3)]}</b>!\n"
                f"{crowd_hype()}\n\n"
                f"{DIVIDER}"
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
        f"⚡ <b>TAG TEAM WARFARE</b>\n"
        f"{DIVIDER}\n\n"
        f"🔴 <b>LEGAL: {n1}</b>\n{hp_bar(hp1)}\n"
        f"↳ <i>Apron: {game['names'][str(partner1)]} ({game['hp'][partner1]} HP)</i>\n\n"
        f"🔵 <b>LEGAL: {n2}</b>\n{hp_bar(hp2)}\n"
        f"↳ <i>Apron: {game['names'][str(partner2)]} ({game['hp'][partner2]} HP)</i>\n\n"
        f"{DIVIDER}\n"
        f"<i>Active superstars, pick your move or tag out:</i>"
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
        await query.answer("You are on the apron! Wait for your partner to tag you.", show_alert=True)
        return

    if move in ["suplex", "rko"] and game["specials"][uid] <= 0:
        await query.answer("No specials left!", show_alert=True)
        return
    if move == "reversal" and game["reversals"][uid] <= 0:
        await query.answer("No reversals left!", show_alert=True)
        return
    if move in ["suplex", "rko", "reversal"] and game["last_move"].get(uid) == move:
        await query.answer("Can't use the same special/reversal consecutively!", show_alert=True)
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
    if m2 == "tag":
        if game["hp"][partner2] > 0:
            lines.append(f"🔄 <b>{n2} TAGS OUT!</b> <b>{game['names'][str(partner2)]}</b> enters the ring!")
            game["active2"] = partner2
            tagged2 = True

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
            lines.append(f"💥 <b>{n1}</b> lands {m1.upper()} for {d1} DMG!")
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

    lines.append(f"\n{DIVIDER}\n{crowd_hype()}")
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

# ---------------- ROYAL RUMBLE ----------------
async def cmd_startrumble(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startrumble inside a group.")
        return
    gid = update.effective_chat.id
    user = update.effective_user
    uid = user.id
    hname = ensure_user(user)

    if gid in games or gid in rumble_games or gid in tag_games:
        await safe_send(update.message.reply_text, "A brawl is already active in this arena.")
        return

    lobbies[gid] = {"host": uid, "players": [uid], "type": "rumble", "max": RUMBLE_MAX_PLAYERS, "message_id": None}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Enter Royal Rumble (1/8)", callback_data=f"join_rumble|{gid}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rumble_lobby|{gid}|{uid}")]
    ])
    msg = await safe_send(
        context.bot.send_message,
        chat_id=gid,
        text=(
            f"👑 <b>ROYAL RUMBLE LOBBY OPENED!</b>\n"
            f"{DIVIDER}\n\n"
            f"Host: <b>{hname}</b>\n"
            f"Needs 4 to 8 superstars. Tap below to draw your number!\n\n"
            f"{DIVIDER}"
        ),
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
    user = query.from_user
    uid = user.id

    lobby = lobbies.get(gid)
    if not lobby or lobby.get("type") != "rumble":
        await query.answer("Rumble lobby expired.", show_alert=True)
        return

    if action == "join_rumble":
        ensure_user(user)
        if uid in lobby["players"]:
            await query.answer("You're already entered in the Rumble!", show_alert=True)
            return
        if len(lobby["players"]) >= RUMBLE_MAX_PLAYERS:
            await query.answer("Royal Rumble is completely full!", show_alert=True)
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
            text=f"👑 <b>ROYAL RUMBLE ENTRIES</b> ({count}/{RUMBLE_MAX_PLAYERS}):\n{DIVIDER}\n" + "\n".join([f"• #{i} <b>{n}</b>" for i, n in enumerate(names, 1)]) + f"\n{DIVIDER}",
            parse_mode=PARSE_MODE,
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )

    elif action == "start_rumble":
        if uid != hid:
            await query.answer("Only the host can start!", show_alert=True)
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
            text=f"🚨 <b>THE BUZZER SOUNDS! 3... 2... 1!</b>\n👑 <b>ROYAL RUMBLE IS UNDERWAY!</b>\nLast superstar standing wins!",
            parse_mode=PARSE_MODE
        )
        await start_rumble_round(gid, context)

    elif action == "cancel_rumble_lobby":
        if uid != hid:
            await query.answer("Only the host can cancel.", show_alert=True)
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
        f"⚡ <b>ROYAL RUMBLE — ROUND {game['round']}</b>\n"
        f"{DIVIDER}\n\n" +
        "\n\n".join(status) +
        f"\n\n{DIVIDER}\n"
        f"🥊 <i>All active superstars, select your move:</i>"
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
        return

    if move == "reversal":
        game["choices"][uid] = {"move": "reversal", "target": None}
        game["last_move"][uid] = "reversal"
        await query.answer("🛡️ Reversal ready! Waiting for others...")
        if len(game["choices"]) == len(game["active"]):
            await resolve_rumble_round(gid, context)
        return

    # Auto-target highest HP opponent or allow DM selection
    opponents = [w for w in game["active"] if w != uid]
    target_opponent = max(opponents, key=lambda w: game["hp"][w])

    game["choices"][uid] = {"move": move, "target": target_opponent}
    game["last_move"][uid] = move
    await query.answer(f"Locked: {move.upper()} on {game['names'][str(target_opponent)]}!")

    if len(game["choices"]) == len(game["active"]):
        await resolve_rumble_round(gid, context)

async def resolve_rumble_round(gid: int, context: ContextTypes.DEFAULT_TYPE):
    game = rumble_games[gid]
    active = game["active"]
    lines = [f"⚡ <b>RUMBLE ROUND {game['round']} CLASH:</b>\n{DIVIDER}\n"]

    damage_taken = {w: 0 for w in active}

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
            lines.append(f"🛡️ <b>{tname} COUNTERS {aname}'s {mv.upper()}!</b> {aname} takes {dmg} DMG!")
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

    lines.append(f"\n{DIVIDER}")
    lines.append(f"{crowd_hype()}")
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
            f"Headlining WrestleMania!\n\n"
            f"{crowd_hype()}"
        )
        user_stats[str(winner)]["wins"] = user_stats[str(winner)].get("wins", 0) + 1
        for w in game["wrestlers"]:
            if w != winner:
                user_stats[str(w)]["losses"] = user_stats[str(w)].get("losses", 0) + 1
    else:
        txt = "🤝 <b>ROYAL RUMBLE DRAW! All remaining wrestlers were eliminated together!</b>"
        for w in game["wrestlers"]:
            user_stats[str(w)]["draws"] = user_stats[str(w)].get("draws", 0) + 1

    save_stats()
    del rumble_games[gid]
    await safe_send(context.bot.send_message, chat_id=gid, text=f"{DIVIDER}\n{txt}\n{DIVIDER}", parse_mode=PARSE_MODE)

# ---------------- FORFEIT & ENDMATCH ----------------
async def cmd_forfeit(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await safe_send(update.message.reply_text, "You forfeited the match.")
    await safe_send(context.bot.send_message, chat_id=target_gid, text=f"⏹️ <b>{lname} threw in the towel!</b>\n🏆 <b>WINNER: {wname}!</b>", parse_mode=PARSE_MODE)

async def cmd_endmatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    uid = update.effective_user.id
    if gid not in games:
        await safe_send(update.message.reply_text, "No active match here.")
        return
    if uid not in games[gid]["players"]:
        await safe_send(update.message.reply_text, "Only match competitors can stop the match.")
        return
    del games[gid]
    await safe_send(update.message.reply_text, "⏹️ <b>Match ended by referee stoppage!</b>", parse_mode=PARSE_MODE)

# ---------------- LIGHTWEIGHT KEEPALIVE WEB SERVER ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("WWE BRAWL BOT 24/7 ONLINE 🔥".encode("utf-8"))

    def log_message(self, format, *args):
        return

def run_keepalive_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), PingHandler)
        logger.info("Web health server listening on port %s", PORT)
        server.serve_forever()
    except Exception as e:
        logger.error("Health server error: %s", e)

# ---------------- INITIALIZATION ----------------
def main():
    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN found.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("startcareer", cmd_startcareer))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
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

    # Background ping server for 24/7 hosting
    threading.Thread(target=run_keepalive_server, daemon=True).start()

    logger.info("⚡ WWE ARENA BOT IS LIVE 24/7!")
    app.run_polling()

if __name__ == "__main__":
    main()
