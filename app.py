import os
import logging
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, ContextTypes, filters
import sqlite3
from html import escape
import asyncio

app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

def get_admin_ids():
    raw = os.environ.get("ADMIN_IDS", "")
    ids = []
    for value in raw.split(","):
        value = value.strip()
        if value.isdigit():
            ids.append(int(value))
    return ids

ADMIN_IDS = get_admin_ids()

DATABASE_PATH = "ostad_bot.db"

def get_connection():
    con = sqlite3.connect(DATABASE_PATH, timeout=15, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con

def normalize_text(text):
    if text is None:
        return None
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = text.strip().replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    return text or None

def generate_professor_code(professor_id):
    return f"OST-{professor_id:04d}"

def init_database():
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS professors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                name TEXT NOT NULL UNIQUE,
                course TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS professor_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                course TEXT,
                requester_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(professor_id) REFERENCES professors(id) ON DELETE CASCADE,
                UNIQUE(professor_id, user_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_professors_name ON professors(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_professor_id ON ratings(professor_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_professor_requests_status ON professor_requests(status)")
        con.commit()
    finally:
        con.close()

def create_professor(name, course):
    name, course = normalize_text(name), normalize_text(course)
    if not name:
        return None
    con = get_connection()
    try:
        cur = con.cursor()
        existing = cur.execute("SELECT id FROM professors WHERE LOWER(name) = LOWER(?) LIMIT 1", (name,)).fetchone()
        if existing:
            return int(existing["id"])
        cur.execute("INSERT INTO professors(name, course) VALUES (?, ?)", (name, course))
        pid = int(cur.lastrowid)
        cur.execute("UPDATE professors SET code = ? WHERE id = ?", (generate_professor_code(pid), pid))
        con.commit()
        return pid
    except:
        con.rollback()
        return None
    finally:
        con.close()

def get_professor_by_id(professor_id):
    con = get_connection()
    try:
        return con.execute("SELECT * FROM professors WHERE id = ?", (professor_id,)).fetchone()
    finally:
        con.close()

def get_professor_by_code(code):
    code = (code or "").strip().upper()
    con = get_connection()
    try:
        return con.execute("SELECT * FROM professors WHERE UPPER(code) = ? LIMIT 1", (code,)).fetchone()
    finally:
        con.close()

def get_all_professors():
    con = get_connection()
    try:
        return con.execute("SELECT * FROM professors ORDER BY name COLLATE NOCASE, id").fetchall()
    finally:
        con.close()

def delete_professor(professor_id):
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM professors WHERE id = ?", (professor_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()

def get_rating_info(professor_id):
    con = get_connection()
    try:
        return con.execute("""
            SELECT COUNT(*) AS total, COALESCE(ROUND(AVG(score), 2), 0) AS average
            FROM ratings WHERE professor_id = ?
        """, (professor_id,)).fetchone()
    finally:
        con.close()

def get_latest_comments(professor_id, limit=5):
    con = get_connection()
    try:
        return con.execute("""
            SELECT score, comment, created_at FROM ratings
            WHERE professor_id = ? AND comment IS NOT NULL AND TRIM(comment) != ''
            ORDER BY datetime(created_at) DESC, id DESC LIMIT ?
        """, (professor_id, limit)).fetchall()
    finally:
        con.close()

def get_user_rating(professor_id, user_id):
    con = get_connection()
    try:
        return con.execute("SELECT * FROM ratings WHERE professor_id = ? AND user_id = ? LIMIT 1", (professor_id, user_id)).fetchone()
    finally:
        con.close()

def save_rating(professor_id, user_id, score, comment):
    if score not in range(1, 6):
        raise ValueError("امتیاز باید بین ۱ تا ۵ باشد.")
    comment = normalize_text(comment)
    con = get_connection()
    try:
        con.execute("""
            INSERT INTO ratings(professor_id, user_id, score, comment)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(professor_id, user_id) DO UPDATE SET
                score = excluded.score,
                comment = excluded.comment,
                created_at = CURRENT_TIMESTAMP
        """, (professor_id, user_id, score, comment))
        con.commit()
    finally:
        con.close()

def create_professor_request(name, course, requester_id):
    name, course = normalize_text(name), normalize_text(course)
    if not name:
        raise ValueError("نام استاد الزامی است.")
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute("INSERT INTO professor_requests(name, course, requester_id) VALUES (?, ?, ?)", (name, course, requester_id))
        rid = int(cur.lastrowid)
        con.commit()
        return rid
    finally:
        con.close()

def get_request(request_id):
    con = get_connection()
    try:
        return con.execute("SELECT * FROM professor_requests WHERE id = ?", (request_id,)).fetchone()
    finally:
        con.close()

def get_pending_requests():
    con = get_connection()
    try:
        return con.execute("SELECT * FROM professor_requests WHERE status = 'pending' ORDER BY id").fetchall()
    finally:
        con.close()

def approve_request(request_id):
    con = get_connection()
    try:
        cur = con.cursor()
        request = cur.execute("SELECT * FROM professor_requests WHERE id = ? AND status = 'pending'", (request_id,)).fetchone()
        if not request:
            return None
        existing = cur.execute("SELECT id FROM professors WHERE LOWER(name) = LOWER(?) LIMIT 1", (request["name"],)).fetchone()
        if existing:
            cur.execute("UPDATE professor_requests SET status = 'approved' WHERE id = ?", (request_id,))
            con.commit()
            return int(existing["id"])
        cur.execute("INSERT INTO professors(name, course) VALUES (?, ?)", (request["name"], request["course"]))
        pid = int(cur.lastrowid)
        cur.execute("UPDATE professors SET code = ? WHERE id = ?", (generate_professor_code(pid), pid))
        cur.execute("UPDATE professor_requests SET status = 'approved' WHERE id = ?", (request_id,))
        con.commit()
        return pid
    except:
        con.rollback()
        raise
    finally:
        con.close()

def reject_request(request_id):
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute("UPDATE professor_requests SET status = 'rejected' WHERE id = ? AND status = 'pending'", (request_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()

ADD_NAME, ADD_COURSE, RATING_SCORE, RATING_COMMENT, PROFESSOR_CODE, ADMIN_ADD_NAME, ADMIN_ADD_COURSE = range(1, 8)
PENDING_PAGE_SIZE = 5

def is_admin(user_id):
    return user_id in ADMIN_IDS

def main_menu_keyboard(user_id):
    rows = [
        [InlineKeyboardButton("📋 لیست اساتید", callback_data="list_professors")],
        [InlineKeyboardButton("🔢 ورود کد استاد", callback_data="enter_code")],
        [InlineKeyboardButton("➕ پیشنهاد استاد جدید", callback_data="student_add")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🔐 پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)

def home_keyboard(user_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")]])

def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن مستقیم استاد", callback_data="admin_add")],
        [InlineKeyboardButton("📨 درخواست‌های در انتظار", callback_data="pending_requests")],
        [InlineKeyboardButton("📋 مدیریت اساتید", callback_data="manage_professors")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")],
    ])

def generate_display_code(pid):
    return f"OST-{pid:04d}"

async def start(update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "🎓 <b>سامانه نظرسنجی اساتید</b>\n\n"
        "از منوی زیر استفاده کنید.\n\n"
        "🔒 امتیازها و نظرات عمومی بدون مشخصات کاربر نمایش داده می‌شوند.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )

async def main_menu(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text(
        "🏠 <b>منوی اصلی</b>\n\nلطفاً یک گزینه را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(q.from_user.id)
    )

async def list_professors(update, context):
    q = update.callback_query
    await q.answer()
    try:
        professors = get_all_professors()
        if not professors:
            await q.edit_message_text(
                "📋 <b>لیست اساتید</b>\n\nهنوز هیچ استادی ثبت نشده است.",
                parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(q.from_user.id)
            )
            return
        text = "📋 <b>لیست اساتید</b>\n\n"
        buttons = []
        for p in professors:
            text += f"👤 <b>{escape(p['name'])}</b>\n📚 درس: <b>{escape(p['course'] or 'ثبت نشده')}</b>\n🔢 کد: <code>{escape(p['code'] or generate_display_code(p['id']))}</code>\n\n"
            buttons.append([InlineKeyboardButton(f"👤 {p['name'][:30]}", callback_data=f"view_prof:{p['id']}")])
        buttons += [[InlineKeyboardButton("🔢 ورود کد استاد", callback_data="enter_code")], [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")]]
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await q.edit_message_text("❌ خطا در دریافت لیست اساتید.\nلطفاً دوباره تلاش کنید.", reply_markup=home_keyboard(q.from_user.id))

async def enter_code_start(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text(
        "🔢 <b>کد استاد را وارد کنید</b>\n\nمثال: <code>OST-0001</code>\n\nبرای لغو /cancel را ارسال کنید.",
        parse_mode=ParseMode.HTML
    )
    return PROFESSOR_CODE

async def receive_code(update, context):
    code = update.message.text.strip()
    professor = get_professor_by_code(code)
    if not professor:
        await update.message.reply_text(
            "❌ این کد پیدا نشد.\nمثلاً <code>OST-0001</code> را وارد کنید.",
            parse_mode=ParseMode.HTML
        )
        return PROFESSOR_CODE
    await send_professor_page(update, professor)
    return ConversationHandler.END

async def view_professor(update, context):
    q = update.callback_query
    await q.answer()
    try:
        pid = int(q.data.split(":", 1)[1])
        p = get_professor_by_id(pid)
        if not p:
            await q.edit_message_text("❌ استاد پیدا نشد.", reply_markup=home_keyboard(q.from_user.id))
            return
        await send_professor_page(update, p)
    except Exception:
        await q.edit_message_text("❌ خطا در نمایش استاد.", reply_markup=home_keyboard(q.from_user.id))

async def send_professor_page(update, professor):
    info = get_rating_info(professor["id"])
    text = f"🎓 <b>{escape(professor['name'])}</b>\n\n📚 درس: <b>{escape(professor['course'] or 'ثبت نشده')}</b>\n🔢 کد: <code>{escape(professor['code'] or generate_display_code(professor['id']))}</code>\n\n"
    if info["total"]:
        text += f"📊 میانگین: <b>{info['average']} از 5</b>\n📝 تعداد امتیازها: <b>{info['total']}</b>\n"
    else:
        text += "📊 هنوز امتیازی ثبت نشده است.\n"
    comments = get_latest_comments(professor["id"], 5)
    if comments:
        text += "\n💬 <b>آخرین نظرات:</b>\n\n"
        for i, c in enumerate(comments, 1):
            text += f"{i}. امتیاز: <b>{c['score']} از 5</b>\n📝 {escape(c['comment'])}\n\n"
    else:
        text += "\n💬 هنوز نظری ثبت نشده است.\n"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 ثبت / ویرایش نظر", callback_data=f"rate:{professor['id']}")],
        [InlineKeyboardButton("📋 لیست اساتید", callback_data="list_professors")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def student_add_start(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text(
        "➕ <b>پیشنهاد استاد جدید</b>\n\nنام و نام خانوادگی استاد را وارد کنید:\n\nبرای لغو /cancel را ارسال کنید.",
        parse_mode=ParseMode.HTML
    )
    return ADD_NAME

async def student_receive_name(update, context):
    name = normalize_text(update.message.text)
    if not name or len(name) < 2:
        await update.message.reply_text("❌ نام معتبر وارد کنید.")
        return ADD_NAME
    context.user_data["new_prof_name"] = name
    await update.message.reply_text("📚 نام درس را وارد کنید:\n\nاگر درس مشخص نیست، «ندارم» را بنویسید.")
    return ADD_COURSE

async def student_receive_course(update, context):
    course = normalize_text(update.message.text)
    if course in {"ندارم", "ندارد", "-", "ندارم."}:
        course = None
    return await finish_student_request(update, context, course)

async def student_skip_course(update, context):
    q = update.callback_query
    await q.answer()
    return await finish_student_request(update, context, None, query=q)

async def finish_student_request(update, context, course, query=None):
    name = context.user_data.get("new_prof_name")
    if not name:
        if query:
            await query.edit_message_text("❌ اطلاعات درخواست ناقص است.", reply_markup=home_keyboard(query.from_user.id))
        return ConversationHandler.END
    try:
        rid = create_professor_request(name, course, update.effective_user.id)
        await notify_admins_about_request(context, rid)
        text = "✅ <b>درخواست شما ثبت شد.</b>\n\nدرخواست پس از بررسی ادمین در لیست اساتید قرار می‌گیرد."
        if query:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=home_keyboard(query.from_user.id))
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=home_keyboard(update.effective_user.id))
    except Exception:
        if query:
            await query.edit_message_text("❌ ثبت درخواست انجام نشد.", reply_markup=home_keyboard(query.from_user.id))
        else:
            await update.message.reply_text("❌ ثبت درخواست انجام نشد.", reply_markup=home_keyboard(update.effective_user.id))
    context.user_data.clear()
    return ConversationHandler.END

async def notify_admins_about_request(context, request_id):
    request = get_request(request_id)
    if not request:
        return
    text = f"📨 <b>درخواست جدید استاد</b>\n\n🆔 درخواست: <code>{request['id']}</code>\n👤 نام: <b>{escape(request['name'])}</b>\n📚 درس: <b>{escape(request['course'] or 'ثبت نشده')}</b>\n👨‍🎓 شناسه درخواست‌دهنده: <code>{request['requester_id']}</code>"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data=f"approve:{request_id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"reject:{request_id}")
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as exc:
            logger.error(f"Could not notify admin {admin_id}: {exc}")

async def rating_start(update, context):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    p = get_professor_by_id(pid)
    if not p:
        await q.edit_message_text("❌ استاد پیدا نشد.", reply_markup=home_keyboard(q.from_user.id))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["rating_professor_id"] = pid
    old = get_user_rating(pid, q.from_user.id)
    prefix = "شما قبلاً برای این استاد امتیاز ثبت کرده‌اید؛ با انتخاب امتیاز جدید، امتیاز قبلی ویرایش می‌شود.\n\n" if old else ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="score:1"), InlineKeyboardButton("2", callback_data="score:2"), InlineKeyboardButton("3", callback_data="score:3")],
        [InlineKeyboardButton("4", callback_data="score:4"), InlineKeyboardButton("5", callback_data="score:5")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel_rating")],
    ])
    await q.edit_message_text(
        f"📝 <b>امتیازدهی به {escape(p['name'])}</b>\n\n{prefix}امتیاز خود را از 1 تا 5 انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    return RATING_SCORE

async def select_score(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["rating_score"] = int(q.data.split(":", 1)[1])
    await q.edit_message_text(
        "💬 <b>نظر خود را بنویسید</b>\n\nیا اگر نمی‌خواهید نظری ثبت کنید، دکمه زیر را بزنید.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("بدون نظر", callback_data="skip_comment")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_rating")]
        ])
    )
    return RATING_COMMENT

async def receive_comment(update, context):
    return await finish_rating(update, context, update.message.text)

async def skip_comment(update, context):
    q = update.callback_query
    await q.answer()
    return await finish_rating(update, context, None, query=q)

async def finish_rating(update, context, comment, query=None):
    pid = context.user_data.get("rating_professor_id")
    score = context.user_data.get("rating_score")
    if not pid or not score:
        msg = "❌ اطلاعات امتیازدهی ناقص است."
        if query:
            await query.edit_message_text(msg, reply_markup=home_keyboard(query.from_user.id))
        else:
            await update.message.reply_text(msg, reply_markup=home_keyboard(update.effective_user.id))
        context.user_data.clear()
        return ConversationHandler.END
    try:
        save_rating(pid, update.effective_user.id, score, comment)
        text = "✅ <b>امتیاز شما ثبت شد.</b>\n\nامتیازدهی شما ناشناس است."
        if query:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎓 مشاهده استاد", callback_data=f"view_prof:{pid}")],
                    [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")]
                ])
            )
        else:
            await update.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎓 مشاهده استاد", callback_data=f"view_prof:{pid}")],
                    [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")]
                ])
            )
    except Exception:
        if query:
            await query.edit_message_text("❌ ثبت امتیاز انجام نشد.", reply_markup=home_keyboard(query.from_user.id))
        else:
            await update.message.reply_text("❌ ثبت امتیاز انجام نشد.", reply_markup=home_keyboard(update.effective_user.id))
    context.user_data.clear()
    return ConversationHandler.END

async def admin_panel(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await q.edit_message_text(
        "🔐 <b>پنل مدیریت</b>\n\nیک گزینه را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard()
    )

async def admin_add_start(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["admin_flow"] = True
    await q.edit_message_text(
        "➕ <b>افزودن مستقیم استاد</b>\n\nنام استاد را وارد کنید:\n\nاین استاد بدون تأیید اضافه می‌شود.",
        parse_mode=ParseMode.HTML
    )
    return ADMIN_ADD_NAME

async def admin_receive_name(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    name = normalize_text(update.message.text)
    if not name or len(name) < 2:
        await update.message.reply_text("❌ نام معتبر وارد کنید.")
        return ADMIN_ADD_NAME
    context.user_data["admin_prof_name"] = name
    await update.message.reply_text("📚 نام درس را وارد کنید یا «ندارم» بنویسید:")
    return ADMIN_ADD_COURSE

async def admin_receive_course(update, context):
    course = normalize_text(update.message.text)
    if course in {"ندارم", "ندارد", "-", "ندارم."}:
        course = None
    return await finish_admin_add(update, context, course)

async def admin_skip_course(update, context):
    q = update.callback_query
    await q.answer()
    return await finish_admin_add(update, context, None, query=q)

async def finish_admin_add(update, context, course, query=None):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    name = context.user_data.get("admin_prof_name")
    try:
        pid = create_professor(name, course)
        if not pid:
            raise RuntimeError("create failed")
        p = get_professor_by_id(pid)
        text = f"✅ <b>استاد با موفقیت اضافه شد.</b>\n\n👤 نام: <b>{escape(p['name'])}</b>\n📚 درس: <b>{escape(p['course'] or 'ثبت نشده')}</b>\n🔢 کد استاد: <code>{escape(p['code'])}</code>"
        if query:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
    except Exception:
        if query:
            await query.edit_message_text("❌ افزودن استاد انجام نشد.", reply_markup=admin_menu_keyboard())
        else:
            await update.message.reply_text("❌ افزودن استاد انجام نشد.", reply_markup=admin_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

async def pending_requests(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    
    page = context.user_data.get("pending_page", 0)
    all_requests = get_pending_requests()
    
    if not all_requests:
        await q.edit_message_text(
            "📨 <b>درخواستی در انتظار نیست.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard()
        )
        return
    
    total_pages = (len(all_requests) + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE
    if page >= total_pages:
        page = total_pages - 1
        context.user_data["pending_page"] = page
    
    start = page * PENDING_PAGE_SIZE
    end = start + PENDING_PAGE_SIZE
    page_requests = all_requests[start:end]
    
    text = f"📨 <b>درخواست‌های در انتظار</b>\n\nصفحه {page + 1} از {total_pages}\n\n"
    buttons = []
    
    for r in page_requests:
        text += f"🆔 <code>{r['id']}</code> | 👤 <b>{escape(r['name'])}</b> | 📚 {escape(r['course'] or 'ثبت نشده')}\n"
        buttons.append([
            InlineKeyboardButton(f"✅ تایید #{r['id']}", callback_data=f"approve:{r['id']}"),
            InlineKeyboardButton(f"❌ رد #{r['id']}", callback_data=f"reject:{r['id']}")
        ])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ قبلی", callback_data="pending_prev"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("بعدی ▶️", callback_data="pending_next"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def pending_next(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["pending_page"] = context.user_data.get("pending_page", 0) + 1
    await pending_requests(update, context)

async def pending_prev(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["pending_page"] = max(0, context.user_data.get("pending_page", 0) - 1)
    await pending_requests(update, context)

async def request_action(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await q.answer()
    action, rid_text = q.data.split(":", 1)
    rid = int(rid_text)
    request = get_request(rid)
    if not request:
        await q.edit_message_text("❌ درخواست پیدا نشد.", reply_markup=admin_menu_keyboard())
        return
    if request["status"] != "pending":
        await q.edit_message_text("⚠️ این درخواست قبلاً بررسی شده است.", reply_markup=admin_menu_keyboard())
        return
    try:
        if action == "approve":
            pid = approve_request(rid)
            if not pid:
                raise RuntimeError("approval failed")
            p = get_professor_by_id(pid)
            text = f"✅ <b>درخواست تأیید شد.</b>\n\n👤 <b>{escape(p['name'])}</b>\n📚 {escape(p['course'] or 'ثبت نشده')}\n🔢 <code>{escape(p['code'])}</code>"
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_keyboard())
            try:
                await context.bot.send_message(
                    request["requester_id"],
                    f"🎉 <b>درخواست استاد شما تأیید شد.</b>\n\n👤 {escape(p['name'])}\n🔢 کد استاد: <code>{escape(p['code'])}</code>",
                    parse_mode=ParseMode.HTML
                )
            except Exception as exc:
                logger.error(f"Requester notification failed: {exc}")
        else:
            ok = reject_request(rid)
            if not ok:
                raise RuntimeError("rejection failed")
            await q.edit_message_text(
                f"❌ <b>درخواست رد شد.</b>\n\n👤 {escape(request['name'])}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu_keyboard()
            )
            try:
                await context.bot.send_message(
                    request["requester_id"],
                    f"❌ درخواست افزودن استاد «{escape(request['name'])}» رد شد.",
                    parse_mode=ParseMode.HTML
                )
            except Exception as exc:
                logger.error(f"Requester notification failed: {exc}")
    except Exception:
        await q.edit_message_text("❌ انجام عملیات ممکن نشد. دوباره تلاش کنید.", reply_markup=admin_menu_keyboard())

async def manage_professors(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    professors = get_all_professors()
    if not professors:
        await q.edit_message_text(
            "📋 <b>هیچ استادی ثبت نشده است.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard()
        )
        return
    text = "📋 <b>مدیریت اساتید</b>\n\nبرای مدیریت یک استاد، آن را انتخاب کنید."
    buttons = [[InlineKeyboardButton(f"👤 {p['name'][:28]} | {p['code']}", callback_data=f"admin_prof:{p['id']}")] for p in professors]
    buttons.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_professor_details(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    pid = int(q.data.split(":", 1)[1])
    p = get_professor_by_id(pid)
    if not p:
        await q.edit_message_text("❌ استاد پیدا نشد.", reply_markup=admin_menu_keyboard())
        return
    info = get_rating_info(pid)
    text = f"👤 <b>اطلاعات استاد</b>\n\nنام: <b>{escape(p['name'])}</b>\nدرس: <b>{escape(p['course'] or 'ثبت نشده')}</b>\nکد: <code>{escape(p['code'])}</code>\nمیانگین: <b>{info['average']} از 5</b>\nتعداد رأی: <b>{info['total']}</b>"
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 حذف استاد", callback_data=f"delete_prof:{pid}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="manage_professors")]
        ])
    )

async def delete_professor(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    pid = int(q.data.split(":", 1)[1])
    p = get_professor_by_id(pid)
    if not p:
        await q.edit_message_text("❌ استاد پیدا نشد.", reply_markup=admin_menu_keyboard())
        return
    await q.edit_message_text(
        f"⚠️ <b>تأیید حذف</b>\n\nآیا «<b>{escape(p['name'])}</b>» حذف شود؟\n\nتمام امتیازها و نظرات او نیز حذف می‌شوند.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ بله، حذف شود", callback_data=f"confirm_delete:{pid}")],
            [InlineKeyboardButton("❌ لغو", callback_data=f"admin_prof:{pid}")]
        ])
    )

async def confirm_delete(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    pid = int(q.data.split(":", 1)[1])
    if delete_professor(pid):
        await q.edit_message_text("✅ استاد با موفقیت حذف شد.", reply_markup=admin_menu_keyboard())
    else:
        await q.edit_message_text("❌ استاد پیدا نشد یا قبلاً حذف شده است.", reply_markup=admin_menu_keyboard())

async def cancel(update, context):
    context.user_data.clear()
    if update.message:
        await update.message.reply_text(
            "❌ عملیات لغو شد.",
            reply_markup=main_menu_keyboard(update.effective_user.id)
        )
    return ConversationHandler.END

async def cancel_callback(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    user_id = q.from_user.id
    if is_admin(user_id) and context.user_data.get("admin_flow"):
        await q.edit_message_text(
            "🔐 <b>پنل مدیریت</b>\n\nیک گزینه را انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard()
        )
    else:
        await q.edit_message_text(
            "❌ عملیات لغو شد.",
            reply_markup=main_menu_keyboard(user_id)
        )
    return ConversationHandler.END

async def run_bot():
    init_database()
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    student_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(student_add_start, pattern=r"^student_add$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_receive_name)],
            ADD_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_receive_course), CallbackQueryHandler(student_skip_course, pattern=r"^student_skip_course$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True, per_user=True,
    )
    code_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_code_start, pattern=r"^enter_code$")],
        states={PROFESSOR_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True, per_user=True,
    )
    rating_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rating_start, pattern=r"^rate:\d+$")],
        states={
            RATING_SCORE: [CallbackQueryHandler(select_score, pattern=r"^score:[1-5]$"), CallbackQueryHandler(cancel_callback, pattern=r"^cancel_rating$")],
            RATING_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment), CallbackQueryHandler(skip_comment, pattern=r"^skip_comment$"), CallbackQueryHandler(cancel_callback, pattern=r"^cancel_rating$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True, per_user=True,
    )
    admin_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern=r"^admin_add$")],
        states={
            ADMIN_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_name)],
            ADMIN_ADD_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_course), CallbackQueryHandler(admin_skip_course, pattern=r"^admin_skip_course$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True, per_user=True,
    )

    for h in (student_add, code_conv, rating_conv, admin_add):
        telegram_app.add_handler(h)

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("cancel", cancel))

    telegram_app.add_handler(CallbackQueryHandler(main_menu, pattern=r"^main_menu$"))
    telegram_app.add_handler(CallbackQueryHandler(list_professors, pattern=r"^list_professors$"))
    telegram_app.add_handler(CallbackQueryHandler(view_professor, pattern=r"^view_prof:\d+$"))

    telegram_app.add_handler(CallbackQueryHandler(admin_panel, pattern=r"^admin_panel$"))
    telegram_app.add_handler(CallbackQueryHandler(pending_requests, pattern=r"^pending_requests$"))
    telegram_app.add_handler(CallbackQueryHandler(pending_next, pattern=r"^pending_next$"))
    telegram_app.add_handler(CallbackQueryHandler(pending_prev, pattern=r"^pending_prev$"))
    telegram_app.add_handler(CallbackQueryHandler(manage_professors, pattern=r"^manage_professors$"))
    telegram_app.add_handler(CallbackQueryHandler(admin_professor_details, pattern=r"^admin_prof:\d+$"))
    telegram_app.add_handler(CallbackQueryHandler(delete_professor, pattern=r"^delete_prof:\d+$"))
    telegram_app.add_handler(CallbackQueryHandler(confirm_delete, pattern=r"^confirm_delete:\d+$"))
    telegram_app.add_handler(CallbackQueryHandler(request_action, pattern=r"^(approve|reject):\d+$"))

    logger.info("🤖 Telegram Ostad Bot is running...")
    await telegram_app.run_polling()

@app.route('/')
def home():
    return "✅ Telegram Ostad Bot is running!"

@app.route('/health')
def health():
    return "OK"

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
