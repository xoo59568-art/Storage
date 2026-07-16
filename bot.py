import os
import logging
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "storage.db")
PAGE_SIZE = 10

# The private group where all uploaded files get archived/stored.
# Get this by adding the bot to the group, then use /groupid inside that group.
STORAGE_GROUP_ID = os.environ.get("STORAGE_GROUP_ID")
STORAGE_GROUP_ID = int(STORAGE_GROUP_ID) if STORAGE_GROUP_ID else None

# Comma-separated Telegram user IDs who are allowed to control the bot
# (needed because private chats with the bot have no "group admin" concept).
# Find your ID with the /myid command.
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_unique_id TEXT,
            file_type TEXT NOT NULL DEFAULT 'video',
            caption TEXT,
            added_by INTEGER,
            added_at TEXT
        )
        """
    )
    # Backfill file_type column if the table already existed from an older version
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
    if "file_type" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN file_type TEXT NOT NULL DEFAULT 'video'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS save_mode (
            chat_id INTEGER PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def is_save_mode_on(chat_id: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT active FROM save_mode WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["active"])


def set_save_mode(chat_id: int, active: bool):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO save_mode (chat_id, active) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET active = excluded.active
        """,
        (chat_id, int(active)),
    )
    conn.commit()
    conn.close()


def save_file(chat_id, file_id, file_unique_id, file_type, caption, added_by):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO videos (chat_id, file_id, file_unique_id, file_type, caption, added_by, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, file_id, file_unique_id, file_type, caption, added_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def count_videos(chat_id) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM videos WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    conn.close()
    return row["c"]


def get_videos_page(chat_id, page: int):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM videos WHERE chat_id = ?
        ORDER BY id ASC LIMIT ? OFFSET ?
        """,
        (chat_id, PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Admin check
# ---------------------------------------------------------------------------
def is_admin_user(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "স্বাগতম! এই বটকে সরাসরি ভিডিও/ফটো/ফাইল পাঠালে বা forward করলে সেটা\n"
        "নির্দিষ্ট স্টোরেজ গ্রুপে জমা হয়ে যাবে।\n\n"
        "কমান্ড সমূহ (শুধু admin ব্যবহার করতে পারবে):\n"
        "/save - সেভ মোড চালু করুন, এরপর ফাইল পাঠান/forward করুন\n"
        "/stopsave - সেভ মোড বন্ধ করুন\n"
        "/files - সেভ করা ফাইল দেখুন (১০টা করে, Next বাটন দিয়ে পরেরগুলো)\n"
        "/stats - মোট কতটা ফাইল সেভ আছে দেখুন\n\n"
        "সেটআপের জন্য:\n"
        "/myid - নিজের Telegram user ID দেখুন (ADMIN_IDS এ বসানোর জন্য)\n"
        "/groupid - এই গ্রুপের ID দেখুন (STORAGE_GROUP_ID এ বসানোর জন্য)"
    )


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"তোমার Telegram user ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def groupid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"এই চ্যাটের ID: `{update.effective_chat.id}`", parse_mode="Markdown")


async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("এই কমান্ড শুধু admin ব্যবহার করতে পারবে।")
        return
    if not STORAGE_GROUP_ID:
        await update.message.reply_text(
            "⚠️ STORAGE_GROUP_ID সেট করা নেই। /groupid দিয়ে গ্রুপের ID বের করে "
            "env variable এ বসান, তারপর বট রিস্টার্ট করুন।"
        )
        return
    set_save_mode(update.effective_chat.id, True)
    await update.message.reply_text(
        "✅ Save mode চালু হয়েছে। এখন যত ভিডিও/ফটো/ফাইল পাঠাবেন বা forward করবেন,\n"
        "সব স্টোরেজ গ্রুপে জমা হবে।\nবন্ধ করতে /stopsave দিন।"
    )


async def stopsave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("এই কমান্ড শুধু admin ব্যবহার করতে পারবে।")
        return
    set_save_mode(update.effective_chat.id, False)
    await update.message.reply_text("⛔ Save mode বন্ধ করা হয়েছে।")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("এই কমান্ড শুধু admin ব্যবহার করতে পারবে।")
        return
    total = count_videos(STORAGE_GROUP_ID) if STORAGE_GROUP_ID else 0
    mode = "চালু ✅" if is_save_mode_on(update.effective_chat.id) else "বন্ধ ⛔"
    await update.message.reply_text(f"মোট সেভ করা ফাইল: {total}\nSave mode (এই চ্যাটে): {mode}")


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_save_mode_on(chat_id):
        return  # save mode off in this chat, ignore

    if not is_admin_user(update.effective_user.id):
        return  # only admin's files get saved

    if not STORAGE_GROUP_ID:
        await update.message.reply_text(
            "⚠️ STORAGE_GROUP_ID সেট করা নেই, তাই ফাইল সেভ করা যাচ্ছে না।"
        )
        return

    msg = update.message
    file_obj = None
    file_type = None

    if msg.video:
        file_obj, file_type = msg.video, "video"
    elif msg.photo:
        file_obj, file_type = msg.photo[-1], "photo"  # highest resolution
    elif msg.document:
        file_obj, file_type = msg.document, "document"
    elif msg.audio:
        file_obj, file_type = msg.audio, "audio"

    if not file_obj:
        return

    # Copy (archive) the message into the storage group, unless it was
    # already sent directly in the storage group itself.
    if chat_id != STORAGE_GROUP_ID:
        try:
            await context.bot.copy_message(
                chat_id=STORAGE_GROUP_ID,
                from_chat_id=chat_id,
                message_id=msg.message_id,
            )
        except Exception as e:
            logger.exception("Failed to copy message to storage group")
            await msg.reply_text(f"⚠️ স্টোরেজ গ্রুপে জমা করা যায়নি: {e}")
            return

    save_file(
        chat_id=STORAGE_GROUP_ID,
        file_id=file_obj.file_id,
        file_unique_id=file_obj.file_unique_id,
        file_type=file_type,
        caption=msg.caption or "",
        added_by=update.effective_user.id,
    )
    await msg.reply_text("✅ সেভ হয়েছে ও স্টোরেজ গ্রুপে জমা হয়েছে।")


async def send_video_page(chat_id, page, context: ContextTypes.DEFAULT_TYPE):
    total = count_videos(STORAGE_GROUP_ID)
    rows = get_videos_page(STORAGE_GROUP_ID, page)

    if not rows:
        await context.bot.send_message(chat_id, "কোনো ফাইল পাওয়া যায়নি।")
        return

    for row in rows:
        file_type = row["file_type"] or "video"
        caption = row["caption"] or None
        if file_type == "video":
            await context.bot.send_video(chat_id=chat_id, video=row["file_id"], caption=caption)
        elif file_type == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=row["file_id"], caption=caption)
        elif file_type == "document":
            await context.bot.send_document(chat_id=chat_id, document=row["file_id"], caption=caption)
        elif file_type == "audio":
            await context.bot.send_audio(chat_id=chat_id, audio=row["file_id"], caption=caption)

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    current_page_num = page + 1

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"vidpage:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"vidpage:{page + 1}"))

    markup = InlineKeyboardMarkup([buttons]) if buttons else None
    info_text = f"পেজ {current_page_num}/{total_pages} • মোট ফাইল: {total}"

    await context.bot.send_message(chat_id, info_text, reply_markup=markup)


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("এই কমান্ড শুধু admin ব্যবহার করতে পারবে।")
        return
    if not STORAGE_GROUP_ID:
        await update.message.reply_text("⚠️ STORAGE_GROUP_ID সেট করা নেই।")
        return
    await send_video_page(update.effective_chat.id, 0, context)


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin_user(update.effective_user.id):
        await query.answer("শুধু admin এটা ব্যবহার করতে পারবে।", show_alert=True)
        return

    await query.answer()
    _, page_str = query.data.split(":")
    page = int(page_str)
    chat_id = update.effective_chat.id

    # Remove buttons from the old message so it can't be double-clicked
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_video_page(chat_id, page, context)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty! No one will be able to use /save, /files, etc. "
            "Set ADMIN_IDS in your .env (comma-separated Telegram user IDs)."
        )
    if not STORAGE_GROUP_ID:
        logger.warning(
            "STORAGE_GROUP_ID is not set! Files can't be archived until you set it. "
            "Add the bot to your storage group, run /groupid there, and set STORAGE_GROUP_ID."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("groupid", groupid_cmd))
    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("stopsave", stopsave_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("videos", files_cmd))  # alias
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.PHOTO | filters.Document.ALL | filters.AUDIO,
            media_handler,
        )
    )
    app.add_handler(CallbackQueryHandler(page_callback, pattern=r"^vidpage:"))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
