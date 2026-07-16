import os
import logging
import sqlite3
import asyncio
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
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

# Delay (seconds) between processing each queued file, to stay under Telegram's
# rate limits when saving large batches (e.g. 1000+ files at once).
QUEUE_DELAY_SECONDS = float(os.environ.get("QUEUE_DELAY_SECONDS", "1.2"))

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
        "/stats - মোট কতটা ফাইল সেভ আছে দেখুন\n"
        "/backup - ডাটাবেজের ব্যাকআপ ফাইল পান (অন্য সার্ভারে নিতে)\n"
        "/restore - ব্যাকআপ ফাইল দিয়ে ডাটা ফিরিয়ে আনুন\n\n"
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
    queue = context.application.bot_data.get("queue")
    queue_size = queue.qsize() if queue else 0
    await update.message.reply_text(
        f"মোট সেভ করা ফাইল: {total}\n"
        f"Save mode (এই চ্যাটে): {mode}\n"
        f"কিউতে অপেক্ষমান ফাইল: {queue_size}"
    )


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("এই কমান্ড শুধু admin ব্যবহার করতে পারবে।")
        return
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("এখনো কোনো ডাটা সেভ হয়নি, ব্যাকআপ করার কিছু নেই।")
        return

    total = count_videos(STORAGE_GROUP_ID) if STORAGE_GROUP_ID else 0
    with open(DB_PATH, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=os.path.basename(DB_PATH),
            caption=(
                f"📦 ব্যাকআপ ফাইল (মোট {total}টা ফাইলের রেফারেন্স আছে এতে)।\n\n"
                "নতুন সার্ভারে migrate করতে:\n"
                "1) এই ফাইলটা ডাউনলোড করে রাখুন\n"
                "2) নতুন সার্ভারে বট বসিয়ে চালু করুন (একই STORAGE_GROUP_ID, ADMIN_IDS সহ .env)\n"
                "3) বটকে /restore কমান্ড দিন, তারপর এই ফাইলটা পাঠান — সব ডাটা ফিরে আসবে"
            ),
        )


async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("এই কমান্ড শুধু admin ব্যবহার করতে পারবে।")
        return
    awaiting = context.application.bot_data.setdefault("awaiting_restore", set())
    awaiting.add(update.effective_user.id)
    await update.message.reply_text(
        "📥 ঠিক আছে, এখন `/backup` দিয়ে পাওয়া সেই ব্যাকআপ ফাইলটা (storage.db) এখানে পাঠান।\n"
        "⚠️ এটা বর্তমান ডাটাবেজ পুরোপুরি বদলে দিবে।",
        parse_mode="Markdown",
    )


async def restore_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    awaiting = context.application.bot_data.get("awaiting_restore", set())

    if user_id not in awaiting or not is_admin_user(user_id):
        return  # not in restore mode, let other handlers (e.g. media_handler) process this
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    tmp_path = DB_PATH + ".restore_tmp"
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(tmp_path)
        os.replace(tmp_path, DB_PATH)
        awaiting.discard(user_id)
        total = count_videos(STORAGE_GROUP_ID) if STORAGE_GROUP_ID else 0
        await update.message.reply_text(
            f"✅ ডাটাবেজ restore সম্পন্ন হয়েছে। মোট {total}টা ফাইলের রেফারেন্স ফিরে পাওয়া গেছে।"
        )
    except Exception as e:
        logger.exception("Restore failed")
        await update.message.reply_text(f"⚠️ Restore ব্যর্থ হয়েছে: {e}")

    # Stop this update from also being handled by media_handler in the next group
    raise ApplicationHandlerStop


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

    item = {
        "origin_chat_id": chat_id,
        "message_id": msg.message_id,
        "file_id": file_obj.file_id,
        "file_unique_id": file_obj.file_unique_id,
        "file_type": file_type,
        "caption": msg.caption or "",
        "added_by": update.effective_user.id,
    }

    queue: asyncio.Queue = context.application.bot_data["queue"]
    pending_counts = context.application.bot_data["pending_counts"]
    batch_active = context.application.bot_data["batch_active"]

    await queue.put(item)
    pending_counts[chat_id] += 1

    if not batch_active.get(chat_id):
        batch_active[chat_id] = True
        await msg.reply_text(
            "📥 ফাইল জমা নেওয়া শুরু হয়েছে। Telegram এর rate limit অনুযায়ী একটু একটু করে "
            "সেভ হবে (অনেক ফাইল হলে সময় লাগতে পারে) — কোনো ফাইল miss হবে না। "
            "সব শেষ হলে আপনাকে জানানো হবে।"
        )


async def queue_worker(application):
    queue: asyncio.Queue = application.bot_data["queue"]
    pending_counts = application.bot_data["pending_counts"]
    batch_active = application.bot_data["batch_active"]
    bot = application.bot

    while True:
        item = await queue.get()
        try:
            if item["origin_chat_id"] != STORAGE_GROUP_ID:
                await bot.copy_message(
                    chat_id=STORAGE_GROUP_ID,
                    from_chat_id=item["origin_chat_id"],
                    message_id=item["message_id"],
                )
            save_file(
                chat_id=STORAGE_GROUP_ID,
                file_id=item["file_id"],
                file_unique_id=item["file_unique_id"],
                file_type=item["file_type"],
                caption=item["caption"],
                added_by=item["added_by"],
            )
        except RetryAfter as e:
            # Telegram is asking us to slow down. Wait, then retry this same item.
            wait_time = e.retry_after + 1
            logger.warning(f"Flood control hit, waiting {wait_time}s before retrying")
            queue.task_done()
            await asyncio.sleep(wait_time)
            await queue.put(item)
            continue
        except TelegramError as e:
            logger.exception(f"Failed to save queued file: {e}")
            pending_counts[item["origin_chat_id"]] = max(
                0, pending_counts[item["origin_chat_id"]] - 1
            )
            try:
                await bot.send_message(
                    item["origin_chat_id"], f"⚠️ একটা ফাইল সেভ করা যায়নি: {e}"
                )
            except Exception:
                pass
            queue.task_done()
            await asyncio.sleep(QUEUE_DELAY_SECONDS)
            continue

        origin = item["origin_chat_id"]
        pending_counts[origin] = max(0, pending_counts[origin] - 1)

        if pending_counts[origin] == 0 and batch_active.get(origin):
            batch_active[origin] = False
            try:
                await bot.send_message(origin, "✅ সব ফাইল সেভ হয়ে গেছে ও স্টোরেজ গ্রুপে জমা হয়েছে।")
            except Exception:
                pass

        queue.task_done()
        await asyncio.sleep(QUEUE_DELAY_SECONDS)


async def on_startup(application):
    application.bot_data["queue"] = asyncio.Queue()
    application.bot_data["pending_counts"] = defaultdict(int)
    application.bot_data["batch_active"] = {}
    asyncio.create_task(queue_worker(application))


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

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("groupid", groupid_cmd))
    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("stopsave", stopsave_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("videos", files_cmd))  # alias
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("restore", restore_cmd))
    # Runs in an earlier group so it can intercept the restore .db upload
    # before the normal media_handler tries to save it as a regular file.
    app.add_handler(MessageHandler(filters.Document.ALL, restore_document_handler), group=-1)
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
