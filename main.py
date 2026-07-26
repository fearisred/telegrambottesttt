import logging
import os
import re
import sqlite3
import signal
import sys
import random
import time
import asyncio
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

DB_PATH = os.environ.get("DB_PATH", "bot_scores.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

LEADERBOARD_LIMIT = 15
    # Stops the same accuser from farming points on the same target by spamming
    # "کسشر" over and over in a row.
SCORE_COOLDOWN_SECONDS = 60

# Random ping configuration
PING_TARGET = "MeSori_Bot"  # Without the @ symbol
PING_CHAT_ID = None  # Set this to a specific chat ID to send to a group, or None to send direct message

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
LEADERBOARD_TRIGGER = "کسشرگویان"

_WORD_BOUNDARY = r"(?:^|\s|[.!؟،,:؛\-—()\"'«»])"

    # Regex to catch the word, allowing for trailing punctuation, a Persian
    # plural/possessive suffix (ه, ها, ی, و ...), or end of string.
TARGET_REGEX = re.compile(
        _WORD_BOUNDARY + re.escape(_TARGET_WORD) + r"(?:ها|های|هایی)?(?=\s|[.!؟،,:؛\-—()\"'«»]|$)",
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

def is_leaderboard_request(text: str) -> bool:
        if not text:
            return False
        normalized = _normalize_persian(text).strip()
        return normalized == LEADERBOARD_TRIGGER

    # ---------------------------------------------------------------------------
    # Database
    # ---------------------------------------------------------------------------
def db_init() -> None:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scores (
                    chat_id    INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    score      INTEGER NOT NULL DEFAULT 0,
                    first_name TEXT,
                    last_name  TEXT,
                    username   TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            conn.commit()
        log.info("Database initialized at %s", DB_PATH)

def db_add_point(chat_id: int, user: User) -> int:
        now = int(time.time())
        first = (user.first_name or "")[:128]
        last = (user.last_name or "")[:128]
        uname = (user.username or "")[:64] if user.username else None

        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute("PRAGMA busy_timeout = 3000")
            conn.execute(
                """
                INSERT INTO scores (chat_id, user_id, score, first_name, last_name, username, updated_at)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    score = score + 1,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    username  = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (chat_id, user.id, first, last, uname, now),
            )
            conn.commit()
            cur = conn.execute(
                "SELECT score FROM scores WHERE chat_id = ? AND user_id = ?",
                (chat_id, user.id),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

def db_get_top(chat_id: int, limit: int = LEADERBOARD_LIMIT):
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cur = conn.execute(
                """
                SELECT user_id, score, first_name, last_name, username
                FROM scores
                WHERE chat_id = ? AND score > 0
                ORDER BY score DESC, updated_at ASC
                LIMIT ?
                """,
                (chat_id, limit),
            )
            return cur.fetchall()

def db_get_score(chat_id: int, user_id: int) -> int:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cur = conn.execute(
                "SELECT score FROM scores WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

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
    # Random ping task
    # ---------------------------------------------------------------------------
async def send_random_ping(application: Application) -> None:
        """Send a random ping message to the target at random intervals (1-24h)."""
        try:
            if PING_CHAT_ID:
                await application.bot.send_message(
                    chat_id=PING_CHAT_ID,
                    text=f"Hi @{PING_TARGET}! 👋"
                )
            else:
                # Try to send as direct message to the bot username
                await application.bot.send_message(
                    chat_id=f"@{PING_TARGET}",
                    text="کص مادرت سوری"
                )
            log.info("Pinged @%s", PING_TARGET)
        except Exception as e:
            log.warning("Failed to send ping to @%s: %s", PING_TARGET, e)

        # Schedule next ping for 1-24 hours from now
        delay_seconds = random.randint(3600, 86400)  # 1 to 24 hours in seconds
        log.info("Next ping scheduled in %d seconds (%.1f hours)", delay_seconds, delay_seconds / 3600)
        await asyncio.sleep(delay_seconds)
        await send_random_ping(application)

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
        except sqlite3.Error as e:
            log.error("DB error: %s", e)
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
        
        # Start the random ping task
        asyncio.create_task(send_random_ping(application))
        
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
        main()

