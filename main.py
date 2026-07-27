import logging
import os
import re
import json
import signal
import sys
import random
import time
from contextlib import closing

from telegram import Update, User
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Set your bot token here, or use the BOT_TOKEN environment variable as fallback.
# Get a token from @BotFather on Telegram.
BOT_TOKEN_HARDCODED = "8821327712:AAErKNxsbPE2F708V_gZoEiB2_recWmgVv4"

BOT_TOKEN = os.environ.get("BOT_TOKEN") or BOT_TOKEN_HARDCODED

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    sys.exit(
        "ERROR: No valid BOT_TOKEN found.\n"
        "Option 1: Set BOT_TOKEN_HARDCODED in main.py\n"
        "Option 2: Run: export BOT_TOKEN=\"your:token-here\"\n"
        "(Get a fresh token from @BotFather — if you had a token hardcoded before, revoke it with /revoke there first)."
    )

DB_PATH = os.environ.get("DB_PATH", "bot_scores.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

LEADERBOARD_LIMIT = 15
# Stops the same accuser from farming points on the same target by spamming
# "کسشر" over and over in a row.
SCORE_COOLDOWN_SECONDS = 60

# Admin who is allowed to reset scores
ADMIN_USER_ID = 1147844656

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kashtar-bot")

# ---------------------------------------------------------------------------
# Persian word matching
# ---------------------------------------------------------------------------
_TARGET_WORD = "کسشر"
_INSULT_WORD = "گاییدمت"
LEADERBOARD_TRIGGER = "کسشرگویان"

_WORD_BOUNDARY = r"(?:^|\s|[.!؟،,:؛\-—()\"'«»])"

# Regex to catch the word, allowing for trailing punctuation, a Persian
# plural/possessive suffix (ه, ها, ی, و ...), or end of string.
TARGET_REGEX = re.compile(
    _WORD_BOUNDARY + re.escape(_TARGET_WORD) + r"(?:ها|های|هایی)?(?=\s|[.!؟،,:؛\-—()\"'«»]|$)",
    re.UNICODE,
)

# Regex for insult word
INSULT_REGEX = re.compile(
    _WORD_BOUNDARY + re.escape(_INSULT_WORD) + r"(?=\s|[.!؟،,:؛\-—()\"'«»]|$)",
    re.UNICODE,
)


def _normalize_persian(s: str) -> str:
    """Normalize common Arabic/Persian character variants."""
    if not s:
        return ""
    replacements = {
        "ي": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "\u0640": "",  # tatweel
    }
    diacritics = "\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652\u0670\u06D6-\u06ED"
    s = re.sub(f"[{diacritics}]", "", s)
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s


def contains_target_word(text: str) -> bool:
    if not text:
        return False
    normalized = _normalize_persian(text)
    return bool(TARGET_REGEX.search(normalized))


def contains_insult_word(text: str) -> bool:
    if not text:
        return False
    normalized = _normalize_persian(text)
    return bool(INSULT_REGEX.search(normalized))


def is_leaderboard_request(text: str) -> bool:
    if not text:
        return False
    normalized = _normalize_persian(text).strip()
    return normalized == LEADERBOARD_TRIGGER

# ---------------------------------------------------------------------------
# Database (JSON-based)
# ---------------------------------------------------------------------------

def _load_db() -> dict:
    """Load scores from JSON file."""
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("Error loading database: %s", e)
            return {}
    return {}


def _save_db(data: dict) -> None:
    """Save scores to JSON file."""
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Error saving database: %s", e)


def db_init() -> None:
    """Initialize database."""
    if not os.path.exists(DB_PATH):
        _save_db({})
    log.info("Database initialized at %s", DB_PATH)


def db_add_point(chat_id: int, user: User) -> int:
    """Add a point to a user in a chat."""
    data = _load_db()
    
    chat_key = str(chat_id)
    user_key = str(user.id)
    
    if chat_key not in data:
        data[chat_key] = {}
    
    if user_key not in data[chat_key]:
        data[chat_key][user_key] = {
            "score": 0,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
            "updated_at": int(time.time()),
        }
    
    data[chat_key][user_key]["score"] += 1
    data[chat_key][user_key]["first_name"] = user.first_name or ""
    data[chat_key][user_key]["last_name"] = user.last_name or ""
    data[chat_key][user_key]["username"] = user.username or ""
    data[chat_key][user_key]["updated_at"] = int(time.time())
    
    _save_db(data)
    return data[chat_key][user_key]["score"]


def db_get_top(chat_id: int, limit: int = LEADERBOARD_LIMIT):
    """Get top users in a chat."""
    data = _load_db()
    chat_key = str(chat_id)
    
    if chat_key not in data:
        return []
    
    users = []
    for user_id, user_data in data[chat_key].items():
        if user_data.get("score", 0) > 0:
            users.append((
                int(user_id),
                user_data["score"],
                user_data.get("first_name", ""),
                user_data.get("last_name", ""),
                user_data.get("username", ""),
            ))
    
    # Sort by score descending, then by updated_at ascending
    users.sort(key=lambda x: (-x[1], data[chat_key][str(x[0])].get("updated_at", 0)))
    return users[:limit]


def db_get_score(chat_id: int, user_id: int) -> int:
    """Get a user's score in a chat."""
    data = _load_db()
    chat_key = str(chat_id)
    user_key = str(user_id)
    
    if chat_key in data and user_key in data[chat_key]:
        return data[chat_key][user_key].get("score", 0)
    return 0


def db_reset_score(chat_id: int, user_id: int) -> int:
    """Reset a user's score in a chat to zero. Returns the previous score (0 if none)."""
    data = _load_db()
    chat_key = str(chat_id)
    user_key = str(user_id)
    
    if chat_key in data and user_key in data[chat_key]:
        prev = data[chat_key][user_key].get("score", 0)
        data[chat_key][user_key]["score"] = 0
        data[chat_key][user_key]["updated_at"] = int(time.time())
        _save_db(data)
        return prev
    return 0

# ---------------------------------------------------------------------------
# Anti-spam: (chat_id, accuser_id, target_id) -> last score timestamp
# ---------------------------------------------------------------------------
_recent_scores: dict[tuple[int, int, int], float] = {}


def _on_cooldown(chat_id: int, accuser_id: int, target_id: int) -> bool:
    key = (chat_id, accuser_id, target_id)
    last = _recent_scores.get(key)
    now = time.monotonic()
    if last is not None and (now - last) < SCORE_COOLDOWN_SECONDS:
        return True
    _recent_scores[key] = now
    return False

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def display_name(first: str, last: str, username: str) -> str:
    parts = [p for p in (first, last) if p]
    name = " ".join(parts).strip() or "ناشناس"
    return _html_escape(name) + (f" (@{_html_escape(username)})" if username else "")

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    message = update.message
    chat_id = message.chat_id
    text = message.text or message.caption or ""

    if is_leaderboard_request(text):
        await send_leaderboard(update, chat_id)
        return

    # Handle insult word
    if contains_insult_word(text):
        await handle_insult(update)
        return

    if not contains_target_word(text):
        return

    replied = message.reply_to_message
    if replied is None:
        return

    target_user = replied.from_user
    accuser = message.from_user
    if target_user is None or target_user.is_bot or accuser is None:
        return

    # Don't let people award themselves points by replying to their own message.
    if target_user.id == accuser.id:
        try:
            await message.reply_text("رو خودت که نمیشه امتیاز داد کصخل😄")
        except Exception:
            pass
        return

    if _on_cooldown(chat_id, accuser.id, target_user.id):
        return  # silently ignore spam-clicking, no need to nag the chat

    try:
        new_score = db_add_point(chat_id, target_user)
    except Exception as e:
        log.error("Error adding point: %s", e)
        return

    accuser_name = accuser.first_name or "یکی"
    target_name = target_user.first_name or "کاربر"

    # All variants keep the same logic: {accuser} just called out {target}
    # for talking nonsense, and {target}'s tally goes up. No mixed signals.
    responses = [
        f"ثبت شد! {accuser_name} گفت {target_name} کسشر گفته 😂\n"
        f" {target_name} رسید به {new_score} امتیاز 🤣",
        f"باز هم {target_name}؟ {accuser_name} امروز حسابش کرد 😏\n"
        f"امتیاز {target_name} الان {new_score} تا شد",
        f"{accuser_name} ردخور داد به {target_name}، کسشر گفتنش رفت تو کسشرگویان 🤣\n"
        f"رکورد جدید: {new_score} امتیاز",
        f"اوه {target_name} جان، {accuser_name} گیر داد بهت 😬\n"
        f"داری میری سمت {new_score} امتیاز، یه کم آروم‌تر برو رو حرفات",
    ]

    try:
        await message.reply_text(random.choice(responses), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Could not send reply: %s", e)


async def handle_insult(update: Update) -> None:
    """Handle گاییدمت insult."""
    if not update.message:
        return
    
    message = update.message
    replied = message.reply_to_message
    if replied is None:
        return

    target_user = replied.from_user
    accuser = message.from_user
    if target_user is None or target_user.is_bot or accuser is None:
        return

    # Don't let people insult themselves
    if target_user.id == accuser.id:
        try:
            await message.reply_text("خودت رو داشتی؟ 😆")
        except Exception:
            pass
        return

    accuser_name = accuser.first_name or "یکی"
    target_name = target_user.first_name or "کاربر"

    responses = [
        f"وای حاجی گاییدت ولی بچه دار نشدی ",
    ]
    try:
        await message.reply_text(random.choice(responses), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Could not send reply: %s", e)


async def send_leaderboard(update: Update, chat_id: int) -> None:
    rows = db_get_top(chat_id, LEADERBOARD_LIMIT)
    if not rows:
        try:
            await update.message.reply_text(
                "فعلا که تو این گروه هیچکس کسشر نگفته! یا شاید خیلی راستگوین 💅\n"
                "برای ثبت، روی پیام کسی که کسشر گفته ریپلای کن و بنویس: کسشر"
            )
        except Exception:
            pass
        return

    headers = [
        "🏆 <b>کسشرگویان</b>\n\n",
        "📋 <b>کسشرگویان گروه</b>\n\n",
        "🚨 <b>لیست کسشرگویان</b>\n\n",
    ]

    lines = [random.choice(headers)]
    medals = ["🥇", "🥈", "🥉"]

    for i, (uid, score, first, last, uname) in enumerate(rows, start=1):
        name = display_name(first or "", last or "", uname or "")
        badge = medals[i - 1] if i <= 3 else f"<b>{i}.</b>"
        lines.append(f"{badge} {name} — <b>{score}</b> امتیاز")

    try:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Could not send leaderboard: %s", e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "سلام بچه‌ها! 👋\n\n"
        "طرز کارم خیلی سادست:\n"
        "• اگه کسی تو گروه کسشر گفت، فقط ریپلای کن روی پیامش و بنویس <b>کسشر</b> تا یه امتیاز ممنوع بگیره 😂\n"
        "• اگه بخوای کسی رو <b>گاییدمت</b> بگویی، روی پیامش ریپلای کن و بنویس <b>گاییدمت</b> 😏\n"
        "• هر وقت خواستید ببینید کی بیشتر کسشر گفته، تو گروه بفرستید: <b>کسشرگویان</b>\n"
        "• برای دیدن امتیاز خودت: /score\n\n"
        "پس مراقب حرفاتون باشید که ثبت نمیشه! 😎"
    )
    try:
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    chat_id = update.message.chat_id
    user = update.message.from_user
    score = db_get_score(chat_id, user.id)

    if score == 0:
        text = "شما فعلا از مچ‌گیری در امونید، امتیازت صفره 👀"
    else:
        text = f"تو تا الان <b>{score}</b> بار تو این گروه کسشر گفته‌ای 😏"

    try:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def cmd_resetscore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only command to reset someone's score in this chat.

    Usage:
    - Reply to a user's message with: /resetscore
    - Or: /resetscore <user_id>

    Only the ADMIN_USER_ID is allowed to run this command.
    """
    if not update.message or not update.message.from_user:
        return

    sender = update.message.from_user
    if sender.id != ADMIN_USER_ID:
        try:
            await update.message.reply_text("شما اجازه انجام این کار را ندارید.")
        except Exception:
            pass
        return

    chat_id = update.message.chat_id

    # Determine target user id: reply -> replied user, else first arg as user_id
    target_id = None
    target_first = None
    target_last = None
    target_username = None

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
        target_id = target.id
        target_first = target.first_name
        target_last = target.last_name
        target_username = target.username
    elif context.args:
        # Expect numeric user id
        try:
            target_id = int(context.args[0])
        except Exception:
            try:
                await update.message.reply_text("استفاده: /resetscore <user_id> یا روی پیام کاربر ریپلای کنید و /resetscore بفرستید")
            except Exception:
                pass
            return
        # Try to fetch name info from JSON if present
        data = _load_db()
        chat_key = str(chat_id)
        user_key = str(target_id)
        if chat_key in data and user_key in data[chat_key]:
            target_first = data[chat_key][user_key].get("first_name", "")
            target_last = data[chat_key][user_key].get("last_name", "")
            target_username = data[chat_key][user_key].get("username", "")

    if not target_id:
        try:
            await update.message.reply_text("هیچ کاربری برای ریست مشخص نشده. از /resetscore <user_id> یا ریپلای استفاده کنید")
        except Exception:
            pass
        return

    try:
        prev = db_reset_score(chat_id, target_id)
    except Exception as e:
        log.error("Error while resetting score: %s", e)
        try:
            await update.message.reply_text("خطا هنگام ریست امتیاز رخ داد.")
        except Exception:
            pass
        return

    name = display_name(target_first or "", target_last or "", target_username or "")
    if prev == 0:
        text = f"امتیاز {name} قبلاً صفر بوده است."
    else:
        text = f"امتیاز {name} ریست شد. قبل از این {prev} امتیاز داشت."

    try:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def on_shutdown(app: Application) -> None:
    log.info("Shutting down.")


def main() -> None:
    db_init()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("score", cmd_score))
    application.add_handler(CommandHandler("resetscore", cmd_resetscore))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message)
    )

    def _handle_signal(signum, _frame):
        log.info("Received signal %s, stopping...", signum)
        try:
            application.stop_running()
        except Exception:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Bot starting (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
