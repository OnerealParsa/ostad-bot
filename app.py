import os
import asyncio
import logging
import sqlite3
import threading
from html import escape

from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "ostad_bot.db")

PROFESSOR_PAGE_SIZE = 5
COMMENT_PAGE_SIZE = 3
PENDING_PAGE_SIZE = 5
ADMIN_PROFESSOR_PAGE_SIZE = 5
MAX_COMMENT_WORDS = 20


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("ostad_bot")


# ============================================================
# ADMIN
# ============================================================

def get_admin_ids():
    raw = os.environ.get("ADMIN_IDS", "").strip()

    if not raw:
        return []

    result = []

    for value in raw.split(","):
        value = value.strip()

        if value.isdigit():
            result.append(int(value))

    return result


ADMIN_IDS = get_admin_ids()


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    con = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        check_same_thread=False,
    )

    con.row_factory = sqlite3.Row

    con.execute("PRAGMA foreign_keys = ON")

    try:
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        logger.warning("Could not enable SQLite WAL mode.")

    return con


def normalize_text(text):
    if text is None:
        return None

    text = str(text)

    text = (
        text
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("ي", "ی")
        .replace("ى", "ی")
        .replace("ك", "ک")
    )

    text = " ".join(text.split())

    return text.strip() or None


def generate_professor_code(professor_id):
    return f"OST-{professor_id:04d}"


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_database():
    con = get_connection()

    try:
        cur = con.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS professors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                name TEXT NOT NULL UNIQUE,
                course TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS professor_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                course TEXT,
                requester_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                professor_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(professor_id)
                    REFERENCES professors(id)
                    ON DELETE CASCADE,
                UNIQUE(professor_id, user_id)
            )
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_professors_name
            ON professors(name)
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ratings_professor_id
            ON ratings(professor_id)
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ratings_created_at
            ON ratings(created_at)
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_professor_requests_status
            ON professor_requests(status)
            """
        )

        con.commit()

    except Exception:
        con.rollback()
        logger.exception("Database initialization failed.")
        raise

    finally:
        con.close()


# ============================================================
# PROFESSORS
# ============================================================

def create_professor(name, course):
    name = normalize_text(name)
    course = normalize_text(course)

    if not name:
        return None

    con = get_connection()

    try:
        cur = con.cursor()

        existing = cur.execute(
            """
            SELECT id
            FROM professors
            WHERE LOWER(name) = LOWER(?)
            LIMIT 1
            """,
            (name,),
        ).fetchone()

        if existing:
            return int(existing["id"])

        cur.execute(
            """
            INSERT INTO professors(name, course)
            VALUES (?, ?)
            """,
            (name, course),
        )

        professor_id = int(cur.lastrowid)

        cur.execute(
            """
            UPDATE professors
            SET code = ?
            WHERE id = ?
            """,
            (
                generate_professor_code(professor_id),
                professor_id,
            ),
        )

        con.commit()

        return professor_id

    except sqlite3.IntegrityError:
        con.rollback()

        existing = con.execute(
            """
            SELECT id
            FROM professors
            WHERE LOWER(name) = LOWER(?)
            LIMIT 1
            """,
            (name,),
        ).fetchone()

        if existing:
            return int(existing["id"])

        logger.exception("Could not create professor.")
        return None

    except Exception:
        con.rollback()
        logger.exception("Could not create professor.")
        return None

    finally:
        con.close()


def get_professor_by_id(professor_id):
    con = get_connection()

    try:
        return con.execute(
            """
            SELECT *
            FROM professors
            WHERE id = ?
            LIMIT 1
            """,
            (professor_id,),
        ).fetchone()

    finally:
        con.close()


def get_professor_by_code(code):
    code = normalize_text(code)

    if not code:
        return None

    con = get_connection()

    try:
        return con.execute(
            """
            SELECT *
            FROM professors
            WHERE UPPER(code) = UPPER(?)
            LIMIT 1
            """,
            (code,),
        ).fetchone()

    finally:
        con.close()


def get_all_professors():
    con = get_connection()

    try:
        return con.execute(
            """
            SELECT *
            FROM professors
            ORDER BY name COLLATE NOCASE ASC, id ASC
            """
        ).fetchall()

    finally:
        con.close()


def delete_professor(professor_id):
    con = get_connection()

    try:
        cur = con.cursor()

        cur.execute(
            """
            DELETE FROM professors
            WHERE id = ?
            """,
            (professor_id,),
        )

        con.commit()

        return cur.rowcount > 0

    except Exception:
        con.rollback()
        logger.exception("Could not delete professor.")
        return False

    finally:
        con.close()


# ============================================================
# RATINGS
# ============================================================

def get_rating_info(professor_id):
    con = get_connection()

    try:
        return con.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(ROUND(AVG(score), 2), 0) AS average
            FROM ratings
            WHERE professor_id = ?
            """,
            (professor_id,),
        ).fetchone()

    finally:
        con.close()


def get_comment_page(professor_id, page=0):
    page = max(0, int(page))

    offset = page * COMMENT_PAGE_SIZE

    con = get_connection()

    try:
        total_row = con.execute(
            """
            SELECT COUNT(*) AS total
            FROM ratings
            WHERE professor_id = ?
              AND comment IS NOT NULL
              AND TRIM(comment) != ''
            """,
            (professor_id,),
        ).fetchone()

        total = int(total_row["total"])

        comments = con.execute(
            """
            SELECT
                id,
                score,
                comment,
                created_at
            FROM ratings
            WHERE professor_id = ?
              AND comment IS NOT NULL
              AND TRIM(comment) != ''
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (
                professor_id,
                COMMENT_PAGE_SIZE,
                offset,
            ),
        ).fetchall()

        total_pages = max(
            1,
            (total + COMMENT_PAGE_SIZE - 1) // COMMENT_PAGE_SIZE,
        )

        if page >= total_pages:
            page = total_pages - 1

            offset = page * COMMENT_PAGE_SIZE

            comments = con.execute(
                """
                SELECT
                    id,
                    score,
                    comment,
                    created_at
                FROM ratings
                WHERE professor_id = ?
                  AND comment IS NOT NULL
                  AND TRIM(comment) != ''
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    professor_id,
                    COMMENT_PAGE_SIZE,
                    offset,
                ),
            ).fetchall()

        return comments, total, total_pages, page

    finally:
        con.close()


def get_user_rating(professor_id, user_id):
    con = get_connection()

    try:
        return con.execute(
            """
            SELECT *
            FROM ratings
            WHERE professor_id = ?
              AND user_id = ?
            LIMIT 1
            """,
            (
                professor_id,
                user_id,
            ),
        ).fetchone()

    finally:
        con.close()


def save_rating(professor_id, user_id, score, comment):
    if score not in range(1, 6):
        raise ValueError("امتیاز باید بین ۱ تا ۵ باشد.")

    comment = normalize_text(comment)

    if comment:
        word_count = len(comment.split())

        if word_count > MAX_COMMENT_WORDS:
            raise ValueError(
                f"نظر نمی‌تواند بیشتر از {MAX_COMMENT_WORDS} کلمه باشد."
            )

    con = get_connection()

    try:
        con.execute(
            """
            INSERT INTO ratings(
                professor_id,
                user_id,
                score,
                comment
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(professor_id, user_id)
            DO UPDATE SET
                score = excluded.score,
                comment = excluded.comment,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                professor_id,
                user_id,
                score,
                comment,
            ),
        )

        con.commit()

    except Exception:
        con.rollback()
        logger.exception("Could not save rating.")
        raise

    finally:
        con.close()


# ============================================================
# PROFESSOR REQUESTS
# ============================================================

def create_professor_request(name, course, requester_id):
    name = normalize_text(name)
    course = normalize_text(course)

    if not name:
        raise ValueError("نام استاد الزامی است.")

    con = get_connection()

    try:
        cur = con.cursor()

        cur.execute(
            """
            INSERT INTO professor_requests(
                name,
                course,
                requester_id
            )
            VALUES (?, ?, ?)
            """,
            (
                name,
                course,
                requester_id,
            ),
        )

        request_id = int(cur.lastrowid)

        con.commit()

        return request_id

    except Exception:
        con.rollback()
        logger.exception("Could not create professor request.")
        raise

    finally:
        con.close()


def get_request(request_id):
    con = get_connection()

    try:
        return con.execute(
            """
            SELECT *
            FROM professor_requests
            WHERE id = ?
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()

    finally:
        con.close()


def get_pending_requests():
    con = get_connection()

    try:
        return con.execute(
            """
            SELECT *
            FROM professor_requests
            WHERE status = 'pending'
            ORDER BY id ASC
            """
        ).fetchall()

    finally:
        con.close()


def approve_request(request_id):
    con = get_connection()

    try:
        cur = con.cursor()

        request = cur.execute(
            """
            SELECT *
            FROM professor_requests
            WHERE id = ?
              AND status = 'pending'
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()

        if not request:
            return None

        existing = cur.execute(
            """
            SELECT id
            FROM professors
            WHERE LOWER(name) = LOWER(?)
            LIMIT 1
            """,
            (request["name"],),
        ).fetchone()

        if existing:
            cur.execute(
                """
                UPDATE professor_requests
                SET status = 'approved'
                WHERE id = ?
                """,
                (request_id,),
            )

            con.commit()

            return int(existing["id"])

        cur.execute(
            """
            INSERT INTO professors(name, course)
            VALUES (?, ?)
            """,
            (
                request["name"],
                request["course"],
            ),
        )

        professor_id = int(cur.lastrowid)

        cur.execute(
            """
            UPDATE professors
            SET code = ?
            WHERE id = ?
            """,
            (
                generate_professor_code(professor_id),
                professor_id,
            ),
        )

        cur.execute(
            """
            UPDATE professor_requests
            SET status = 'approved'
            WHERE id = ?
            """,
            (request_id,),
        )

        con.commit()

        return professor_id

    except Exception:
        con.rollback()
        logger.exception("Could not approve request.")
        raise

    finally:
        con.close()


def reject_request(request_id):
    con = get_connection()

    try:
        cur = con.cursor()

        cur.execute(
            """
            UPDATE professor_requests
            SET status = 'rejected'
            WHERE id = ?
              AND status = 'pending'
            """,
            (request_id,),
        )

        con.commit()

        return cur.rowcount > 0

    except Exception:
        con.rollback()
        logger.exception("Could not reject request.")
        return False

    finally:
        con.close()


# ============================================================
# CONVERSATION STATES
# ============================================================

(
    ADD_NAME,
    ADD_COURSE,
    RATING_SCORE,
    RATING_COMMENT,
    PROFESSOR_CODE,
    ADMIN_ADD_NAME,
    ADMIN_ADD_COURSE,
) = range(1, 8)


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu_keyboard(user_id):
    rows = [
        [
            InlineKeyboardButton(
                "📋 لیست اساتید",
                callback_data="list_professors:0",
            )
        ],
        [
            InlineKeyboardButton(
                "🔢 ورود کد استاد",
                callback_data="enter_code",
            )
        ],
        [
            InlineKeyboardButton(
                "➕ پیشنهاد استاد جدید",
                callback_data="student_add",
            )
        ],
    ]

    if is_admin(user_id):
        rows.append(
            [
                InlineKeyboardButton(
                    "🔐 پنل مدیریت",
                    callback_data="admin_panel",
                )
            ]
        )

    return InlineKeyboardMarkup(rows)


def home_keyboard(user_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏠 منوی اصلی",
                    callback_data="main_menu",
                )
            ]
        ]
    )


def admin_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ افزودن مستقیم استاد",
                    callback_data="admin_add",
                )
            ],
            [
                InlineKeyboardButton(
                    "📨 درخواست‌های در انتظار",
                    callback_data="pending_requests:0",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 مدیریت اساتید",
                    callback_data="manage_professors:0",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 منوی اصلی",
                    callback_data="main_menu",
                )
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    if not update.message:
        return

    await update.message.reply_text(
        "🎓 <b>سامانه نظرسنجی اساتید</b>\n\n"
        "از منوی زیر استفاده کنید.\n\n"
        "🔒 امتیازها و نظرات عمومی بدون مشخصات کاربر نمایش داده می‌شوند.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(
            update.effective_user.id
        ),
    )


# ============================================================
# MAIN MENU
# ============================================================

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    user_id = q.from_user.id

    await q.answer()

    context.user_data.clear()

    await q.edit_message_text(
        "🏠 <b>منوی اصلی</b>\n\n"
        "لطفاً یک گزینه را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user_id),
    )


# ============================================================
# PROFESSOR LIST
# ============================================================

async def list_professors(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    try:
        professors = get_all_professors()

        if not professors:
            await q.edit_message_text(
                "📋 <b>لیست اساتید</b>\n\n"
                "هنوز هیچ استادی ثبت نشده است.",
                parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(q.from_user.id),
            )
            return

        try:
            page = int(q.data.split(":")[1])
        except (IndexError, ValueError):
            page = 0

        total = len(professors)

        total_pages = max(
            1,
            (total + PROFESSOR_PAGE_SIZE - 1)
            // PROFESSOR_PAGE_SIZE,
        )

        page = max(
            0,
            min(page, total_pages - 1),
        )

        start_index = page * PROFESSOR_PAGE_SIZE
        end_index = start_index + PROFESSOR_PAGE_SIZE

        page_professors = professors[
            start_index:end_index
        ]

        text = (
            "📋 <b>لیست اساتید</b>\n\n"
            f"صفحه <b>{page + 1}</b> از <b>{total_pages}</b>\n\n"
        )

        buttons = []

        for professor in page_professors:
            name = escape(
                professor["name"]
            )

            course = escape(
                professor["course"]
                or "درس ثبت نشده"
            )

            code = escape(
                professor["code"]
                or generate_professor_code(
                    professor["id"]
                )
            )

            text += (
                f"👤 <b>{name}</b>\n"
                f"📚 درس: <b>{course}</b>\n"
                f"🔢 کد: <code>{code}</code>\n\n"
            )

            button_text = (
                f"👤 {professor['name'][:25]}"
            )

            buttons.append(
                [
                    InlineKeyboardButton(
                        button_text,
                        callback_data=(
                            f"view_prof:{professor['id']}:0"
                        ),
                    )
                ]
            )

        navigation = []

        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "◀️ قبلی",
                    callback_data=(
                        f"list_professors:{page - 1}"
                    ),
                )
            )

        if page < total_pages - 1:
            navigation.append(
                InlineKeyboardButton(
                    "بعدی ▶️",
                    callback_data=(
                        f"list_professors:{page + 1}"
                    ),
                )
            )

        if navigation:
            buttons.append(navigation)

        buttons.append(
            [
                InlineKeyboardButton(
                    "🔢 ورود کد استاد",
                    callback_data="enter_code",
                )
            ]
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    "🏠 منوی اصلی",
                    callback_data="main_menu",
                )
            ]
        )

        await q.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except Exception:
        logger.exception("Error while showing professor list.")

        await q.edit_message_text(
            "❌ خطا در دریافت لیست اساتید.\n"
            "لطفاً دوباره تلاش کنید.",
            reply_markup=home_keyboard(
                q.from_user.id
            ),
        )


# ============================================================
# ENTER PROFESSOR CODE
# ============================================================

async def enter_code_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    context.user_data.clear()

    await q.edit_message_text(
        "🔢 <b>کد استاد را وارد کنید</b>\n\n"
        "مثال:\n"
        "<code>OST-0001</code>\n\n"
        "برای لغو /cancel را ارسال کنید.",
        parse_mode=ParseMode.HTML,
    )

    return PROFESSOR_CODE


async def receive_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    code = normalize_text(update.message.text)

    professor = get_professor_by_code(code)

    if not professor:
        await update.message.reply_text(
            "❌ این کد پیدا نشد.\n\n"
            "مثلاً:\n"
            "<code>OST-0001</code>",
            parse_mode=ParseMode.HTML,
        )

        return PROFESSOR_CODE

    await send_professor_page(
        update,
        professor,
        comment_page=0,
    )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# PROFESSOR DETAIL
# ============================================================

async def view_professor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    try:
        parts = q.data.split(":")

        professor_id = int(parts[1])

        comment_page = 0

        if len(parts) >= 3:
            try:
                comment_page = int(parts[2])
            except ValueError:
                comment_page = 0

        professor = get_professor_by_id(
            professor_id
        )

        if not professor:
            await q.edit_message_text(
                "❌ استاد پیدا نشد.",
                reply_markup=home_keyboard(
                    q.from_user.id
                ),
            )
            return

        await send_professor_page(
            update,
            professor,
            comment_page=comment_page,
        )

    except Exception:
        logger.exception(
            "Error while showing professor details."
        )

        await q.edit_message_text(
            "❌ خطا در نمایش اطلاعات استاد.",
            reply_markup=home_keyboard(
                q.from_user.id
            ),
        )


async def send_professor_page(
    update: Update,
    professor,
    comment_page=0,
):
    professor_id = int(professor["id"])

    info = get_rating_info(
        professor_id
    )

    comments, total_comments, total_comment_pages, actual_page = (
        get_comment_page(
            professor_id,
            comment_page,
        )
    )

    name = escape(
        professor["name"]
    )

    course = escape(
        professor["course"]
        or "ثبت نشده"
    )

    code = escape(
        professor["code"]
        or generate_professor_code(
            professor_id
        )
    )

    text = (
        f"🎓 <b>{name}</b>\n\n"
        f"📚 درس: <b>{course}</b>\n"
        f"🔢 کد: <code>{code}</code>\n\n"
    )

    if info["total"]:
        text += (
            f"📊 میانگین امتیاز: "
            f"<b>{info['average']} از 5</b>\n"
            f"📝 تعداد امتیازها: "
            f"<b>{info['total']}</b>\n"
        )
    else:
        text += (
            "📊 هنوز امتیازی ثبت نشده است.\n"
        )

    if comments:
        text += (
            "\n💬 <b>آخرین نظرات</b>\n\n"
        )

        for index, comment in enumerate(
            comments,
            start=1,
        ):
            comment_text = escape(
                comment["comment"]
            )

            text += (
                f"{index}. ⭐ <b>"
                f"{comment['score']} از 5"
                f"</b>\n"
                f"📝 {comment_text}\n\n"
            )

        if total_comment_pages > 1:
            text += (
                f"صفحه نظرات: "
                f"<b>{actual_page + 1}</b> "
                f"از "
                f"<b>{total_comment_pages}</b>\n"
            )

    else:
        text += (
            "\n💬 هنوز نظری ثبت نشده است.\n"
        )

    buttons = [
        [
            InlineKeyboardButton(
                "📝 ثبت / ویرایش نظر",
                callback_data=(
                    f"rate:{professor_id}"
                ),
            )
        ]
    ]

    if total_comment_pages > 1:
        navigation = []

        if actual_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "◀️ نظرات جدیدتر",
                    callback_data=(
                        f"view_prof:{professor_id}:"
                        f"{actual_page - 1}"
                    ),
                )
            )

        if actual_page < total_comment_pages - 1:
            navigation.append(
                InlineKeyboardButton(
                    "نظرات قدیمی‌تر ▶️",
                    callback_data=(
                        f"view_prof:{professor_id}:"
                        f"{actual_page + 1}"
                    ),
                )
            )

        if navigation:
            buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                "📋 لیست اساتید",
                callback_data="list_professors:0",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "🏠 منوی اصلی",
                callback_data="main_menu",
            )
        ]
    )

    markup = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as exc:
            if "Message is not modified" not in str(exc):
                raise

    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


# ============================================================
# STUDENT ADD PROFESSOR
# ============================================================

async def student_add_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    context.user_data.clear()

    await q.edit_message_text(
        "➕ <b>پیشنهاد استاد جدید</b>\n\n"
        "نام و نام خانوادگی استاد را وارد کنید.\n\n"
        "برای لغو /cancel را ارسال کنید.",
        parse_mode=ParseMode.HTML,
    )

    return ADD_NAME


async def student_receive_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = normalize_text(
        update.message.text
    )

    if not name or len(name) < 2:
        await update.message.reply_text(
            "❌ نام معتبر وارد کنید."
        )
        return ADD_NAME

    context.user_data["new_prof_name"] = name

    await update.message.reply_text(
        "📚 نام درس را وارد کنید.\n\n"
        "اگر درس مشخص نیست، «ندارم» را بنویسید."
    )

    return ADD_COURSE


async def student_receive_course(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    course = normalize_text(
        update.message.text
    )

    if course in {
        "ندارم",
        "ندارد",
        "-",
        "ندارم.",
        "ندارم ",
    }:
        course = None

    return await finish_student_request(
        update,
        context,
        course,
    )


async def finish_student_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    course,
):
    name = context.user_data.get(
        "new_prof_name"
    )

    if not name:
        await update.message.reply_text(
            "❌ اطلاعات درخواست ناقص است.",
            reply_markup=home_keyboard(
                update.effective_user.id
            ),
        )

        context.user_data.clear()

        return ConversationHandler.END

    try:
        request_id = create_professor_request(
            name,
            course,
            update.effective_user.id,
        )

        await notify_admins_about_request(
            context,
            request_id,
        )

        await update.message.reply_text(
            "✅ <b>درخواست شما ثبت شد.</b>\n\n"
            "درخواست پس از بررسی ادمین "
            "در لیست اساتید قرار می‌گیرد.",
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(
                update.effective_user.id
            ),
        )

    except Exception:
        logger.exception(
            "Could not finish student request."
        )

        await update.message.reply_text(
            "❌ ثبت درخواست انجام نشد.\n"
            "لطفاً دوباره تلاش کنید.",
            reply_markup=home_keyboard(
                update.effective_user.id
            ),
        )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# ADMIN NOTIFICATION
# ============================================================

async def notify_admins_about_request(
    context: ContextTypes.DEFAULT_TYPE,
    request_id,
):
    request = get_request(request_id)

    if not request:
        return

    text = (
        "📨 <b>درخواست جدید استاد</b>\n\n"
        f"🆔 درخواست: "
        f"<code>{request['id']}</code>\n"
        f"👤 نام: "
        f"<b>{escape(request['name'])}</b>\n"
        f"📚 درس: "
        f"<b>{escape(request['course'] or 'ثبت نشده')}</b>\n"
        f"👨‍🎓 شناسه درخواست‌دهنده: "
        f"<code>{request['requester_id']}</code>"
    )

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تأیید",
                    callback_data=(
                        f"approve:{request_id}"
                    ),
                ),
                InlineKeyboardButton(
                    "❌ رد",
                    callback_data=(
                        f"reject:{request_id}"
                    ),
                ),
            ]
        ]
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )

        except Exception:
            logger.exception(
                "Could not notify admin %s",
                admin_id,
            )


# ============================================================
# RATING
# ============================================================

async def rating_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    try:
        professor_id = int(
            q.data.split(":", 1)[1]
        )
    except (IndexError, ValueError):
        await q.edit_message_text(
            "❌ اطلاعات استاد نامعتبر است.",
            reply_markup=home_keyboard(
                q.from_user.id
            ),
        )

        return ConversationHandler.END

    professor = get_professor_by_id(
        professor_id
    )

    if not professor:
        await q.edit_message_text(
            "❌ استاد پیدا نشد.",
            reply_markup=home_keyboard(
                q.from_user.id
            ),
        )

        return ConversationHandler.END

    context.user_data.clear()

    context.user_data[
        "rating_professor_id"
    ] = professor_id

    old_rating = get_user_rating(
        professor_id,
        q.from_user.id,
    )

    if old_rating:
        prefix = (
            "⚠️ شما قبلاً برای این استاد "
            "امتیاز ثبت کرده‌اید.\n"
            "با انتخاب امتیاز جدید، "
            "امتیاز قبلی ویرایش می‌شود.\n\n"
        )
    else:
        prefix = ""

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "1 ⭐",
                    callback_data="score:1",
                ),
                InlineKeyboardButton(
                    "2 ⭐⭐",
                    callback_data="score:2",
                ),
                InlineKeyboardButton(
                    "3 ⭐⭐⭐",
                    callback_data="score:3",
                ),
            ],
            [
                InlineKeyboardButton(
                    "4 ⭐⭐⭐⭐",
                    callback_data="score:4",
                ),
                InlineKeyboardButton(
                    "5 ⭐⭐⭐⭐⭐",
                    callback_data="score:5",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data="cancel_rating",
                )
            ],
        ]
    )

    await q.edit_message_text(
        f"📝 <b>امتیازدهی به "
        f"{escape(professor['name'])}</b>\n\n"
        f"{prefix}"
        "امتیاز خود را از 1 تا 5 انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )

    return RATING_SCORE


async def select_score(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    score = int(
        q.data.split(":", 1)[1]
    )

    context.user_data[
        "rating_score"
    ] = score

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "بدون نظر",
                    callback_data="skip_comment",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data="cancel_rating",
                )
            ],
        ]
    )

    await q.edit_message_text(
        "💬 <b>نظر خود را بنویسید</b>\n\n"
        f"حداکثر <b>{MAX_COMMENT_WORDS} کلمه</b>.\n\n"
        "اگر نمی‌خواهید نظری ثبت کنید، "
        "«بدون نظر» را بزنید.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )

    return RATING_COMMENT


async def receive_comment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    comment = normalize_text(
        update.message.text
    )

    if not comment:
        await update.message.reply_text(
            "❌ متن نظر نمی‌تواند خالی باشد."
        )

        return RATING_COMMENT

    word_count = len(
        comment.split()
    )

    if word_count > MAX_COMMENT_WORDS:
        await update.message.reply_text(
            "❌ <b>نظر شما بیشتر از حد مجاز است.</b>\n\n"
            f"تعداد کلمات: <b>{word_count}</b>\n"
            f"حداکثر مجاز: <b>{MAX_COMMENT_WORDS}</b>\n\n"
            "لطفاً نظر را کوتاه‌تر کنید.",
            parse_mode=ParseMode.HTML,
        )

        return RATING_COMMENT

    return await finish_rating(
        update,
        context,
        comment,
    )


async def skip_comment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    await q.answer()

    return await finish_rating(
        update,
        context,
        None,
        query=q,
    )


async def finish_rating(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    comment,
    query=None,
):
    professor_id = context.user_data.get(
        "rating_professor_id"
    )

    score = context.user_data.get(
        "rating_score"
    )

    if not professor_id or not score:
        text = (
            "❌ اطلاعات امتیازدهی ناقص است."
        )

        markup = home_keyboard(
            update.effective_user.id
        )

        if query:
            await query.edit_message_text(
                text,
                reply_markup=markup,
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=markup,
            )

        context.user_data.clear()

        return ConversationHandler.END

    try:
        save_rating(
            professor_id,
            update.effective_user.id,
            score,
            comment,
        )

        text = (
            "✅ <b>امتیاز شما ثبت شد.</b>\n\n"
            "امتیازدهی شما ناشناس است."
        )

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎓 مشاهده استاد",
                        callback_data=(
                            f"view_prof:{professor_id}:0"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 منوی اصلی",
                        callback_data="main_menu",
                    )
                ],
            ]
        )

        if query:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        else:
            await update.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )

    except ValueError as exc:
        if query:
            await query.edit_message_text(
                f"❌ {escape(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(
                    update.effective_user.id
                ),
            )
        else:
            await update.message.reply_text(
                f"❌ {escape(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(
                    update.effective_user.id
                ),
            )

        return RATING_COMMENT

    except Exception:
        logger.exception(
            "Could not finish rating."
        )

        if query:
            await query.edit_message_text(
                "❌ ثبت امتیاز انجام نشد.\n"
                "لطفاً دوباره تلاش کنید.",
                reply_markup=home_keyboard(
                    update.effective_user.id
                ),
            )
        else:
            await update.message.reply_text(
                "❌ ثبت امتیاز انجام نشد.\n"
                "لطفاً دوباره تلاش کنید.",
                reply_markup=home_keyboard(
                    update.effective_user.id
                ),
            )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await q.answer()

    await q.edit_message_text(
        "🔐 <b>پنل مدیریت</b>\n\n"
        "یک گزینه را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )


# ============================================================
# ADMIN ADD
# ============================================================

async def admin_add_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return ConversationHandler.END

    await q.answer()

    context.user_data.clear()

    context.user_data[
        "admin_flow"
    ] = True

    await q.edit_message_text(
        "➕ <b>افزودن مستقیم استاد</b>\n\n"
        "نام استاد را وارد کنید.\n\n"
        "این استاد بدون تأیید اضافه می‌شود.\n\n"
        "برای لغو /cancel را ارسال کنید.",
        parse_mode=ParseMode.HTML,
    )

    return ADMIN_ADD_NAME


async def admin_receive_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(
        update.effective_user.id
    ):
        return ConversationHandler.END

    name = normalize_text(
        update.message.text
    )

    if not name or len(name) < 2:
        await update.message.reply_text(
            "❌ نام معتبر وارد کنید."
        )

        return ADMIN_ADD_NAME

    context.user_data[
        "admin_prof_name"
    ] = name

    await update.message.reply_text(
        "📚 نام درس را وارد کنید.\n\n"
        "اگر مشخص نیست، «ندارم» بنویسید."
    )

    return ADMIN_ADD_COURSE


async def admin_receive_course(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(
        update.effective_user.id
    ):
        return ConversationHandler.END

    course = normalize_text(
        update.message.text
    )

    if course in {
        "ندارم",
        "ندارد",
        "-",
        "ندارم.",
    }:
        course = None

    return await finish_admin_add(
        update,
        context,
        course,
    )


async def finish_admin_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    course,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        context.user_data.clear()
        return ConversationHandler.END

    name = context.user_data.get(
        "admin_prof_name"
    )

    if not name:
        await update.message.reply_text(
            "❌ اطلاعات ناقص است.",
            reply_markup=admin_menu_keyboard(),
        )

        context.user_data.clear()

        return ConversationHandler.END

    try:
        professor_id = create_professor(
            name,
            course,
        )

        if not professor_id:
            raise RuntimeError(
                "Professor creation failed."
            )

        professor = get_professor_by_id(
            professor_id
        )

        text = (
            "✅ <b>استاد با موفقیت اضافه شد.</b>\n\n"
            f"👤 نام: "
            f"<b>{escape(professor['name'])}</b>\n"
            f"📚 درس: "
            f"<b>{escape(professor['course'] or 'ثبت نشده')}</b>\n"
            f"🔢 کد استاد: "
            f"<code>{escape(professor['code'])}</code>"
        )

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )

    except Exception:
        logger.exception(
            "Could not add professor from admin."
        )

        await update.message.reply_text(
            "❌ افزودن استاد انجام نشد.",
            reply_markup=admin_menu_keyboard(),
        )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# PENDING REQUESTS
# ============================================================

async def pending_requests(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page=None,
    answer_callback=True,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        if answer_callback:
            await q.answer(
                "❌ دسترسی ندارید.",
                show_alert=True,
            )
        return

    if answer_callback:
        await q.answer()

    requests = get_pending_requests()

    if not requests:
        await q.edit_message_text(
            "📨 <b>درخواستی در انتظار نیست.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        return

    if page is None:
        try:
            page = int(
                q.data.split(":")[1]
            )
        except (IndexError, ValueError):
            page = 0

    total = len(requests)

    total_pages = max(
        1,
        (total + PENDING_PAGE_SIZE - 1)
        // PENDING_PAGE_SIZE,
    )

    page = max(
        0,
        min(page, total_pages - 1),
    )

    start_index = (
        page * PENDING_PAGE_SIZE
    )

    end_index = (
        start_index + PENDING_PAGE_SIZE
    )

    page_requests = requests[
        start_index:end_index
    ]

    text = (
        "📨 <b>درخواست‌های در انتظار</b>\n\n"
        f"صفحه <b>{page + 1}</b> از "
        f"<b>{total_pages}</b>\n\n"
    )

    buttons = []

    for request in page_requests:
        text += (
            f"🆔 <code>{request['id']}</code>\n"
            f"👤 <b>{escape(request['name'])}</b>\n"
            f"📚 {escape(request['course'] or 'ثبت نشده')}\n\n"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ تأیید #{request['id']}",
                    callback_data=(
                        f"approve:{request['id']}"
                    ),
                ),
                InlineKeyboardButton(
                    f"❌ رد #{request['id']}",
                    callback_data=(
                        f"reject:{request['id']}"
                    ),
                ),
            ]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️ قبلی",
                callback_data=(
                    f"pending_requests:{page - 1}"
                ),
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=(
                    f"pending_requests:{page + 1}"
                ),
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                "🔙 پنل مدیریت",
                callback_data="admin_panel",
            )
        ]
    )

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


# ============================================================
# REQUEST ACTION
# ============================================================

async def request_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await q.answer()

    try:
        action, request_id_text = (
            q.data.split(":", 1)
        )

        request_id = int(
            request_id_text
        )

    except (ValueError, IndexError):
        await q.edit_message_text(
            "❌ درخواست نامعتبر است.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    request = get_request(
        request_id
    )

    if not request:
        await q.edit_message_text(
            "❌ درخواست پیدا نشد.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if request["status"] != "pending":
        await q.edit_message_text(
            "⚠️ این درخواست قبلاً بررسی شده است.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    try:
        if action == "approve":
            professor_id = approve_request(
                request_id
            )

            if not professor_id:
                raise RuntimeError(
                    "Approval failed."
                )

            professor = get_professor_by_id(
                professor_id
            )

            text = (
                "✅ <b>درخواست تأیید شد.</b>\n\n"
                f"👤 <b>{escape(professor['name'])}</b>\n"
                f"📚 {escape(professor['course'] or 'ثبت نشده')}\n"
                f"🔢 <code>{escape(professor['code'])}</code>"
            )

            await q.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu_keyboard(),
            )

            try:
                await context.bot.send_message(
                    chat_id=request["requester_id"],
                    text=(
                        "🎉 <b>درخواست استاد شما تأیید شد.</b>\n\n"
                        f"👤 {escape(professor['name'])}\n"
                        f"📚 {escape(professor['course'] or 'ثبت نشده')}\n"
                        f"🔢 کد استاد: "
                        f"<code>{escape(professor['code'])}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )

            except Exception:
                logger.exception(
                    "Requester approval notification failed."
                )

        elif action == "reject":
            success = reject_request(
                request_id
            )

            if not success:
                raise RuntimeError(
                    "Rejection failed."
                )

            await q.edit_message_text(
                (
                    "❌ <b>درخواست رد شد.</b>\n\n"
                    f"👤 {escape(request['name'])}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu_keyboard(),
            )

            try:
                await context.bot.send_message(
                    chat_id=request["requester_id"],
                    text=(
                        "❌ <b>درخواست افزودن استاد رد شد.</b>\n\n"
                        f"👤 {escape(request['name'])}"
                    ),
                    parse_mode=ParseMode.HTML,
                )

            except Exception:
                logger.exception(
                    "Requester rejection notification failed."
                )

    except Exception:
        logger.exception(
            "Could not process professor request."
        )

        await q.edit_message_text(
            "❌ انجام عملیات ممکن نشد.\n"
            "دوباره تلاش کنید.",
            reply_markup=admin_menu_keyboard(),
        )


# ============================================================
# ADMIN PROFESSOR MANAGEMENT
# ============================================================

async def manage_professors(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page=None,
    answer_callback=True,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        if answer_callback:
            await q.answer(
                "❌ دسترسی ندارید.",
                show_alert=True,
            )
        return

    if answer_callback:
        await q.answer()

    professors = get_all_professors()

    if not professors:
        await q.edit_message_text(
            "📋 <b>هیچ استادی ثبت نشده است.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        return

    if page is None:
        try:
            page = int(
                q.data.split(":")[1]
            )
        except (IndexError, ValueError):
            page = 0

    total = len(professors)

    total_pages = max(
        1,
        (
            total
            + ADMIN_PROFESSOR_PAGE_SIZE
            - 1
        )
        // ADMIN_PROFESSOR_PAGE_SIZE,
    )

    page = max(
        0,
        min(page, total_pages - 1),
    )

    start_index = (
        page * ADMIN_PROFESSOR_PAGE_SIZE
    )

    end_index = (
        start_index
        + ADMIN_PROFESSOR_PAGE_SIZE
    )

    page_professors = professors[
        start_index:end_index
    ]

    text = (
        "📋 <b>مدیریت اساتید</b>\n\n"
        f"صفحه <b>{page + 1}</b> از "
        f"<b>{total_pages}</b>\n\n"
        "برای مدیریت یک استاد، آن را انتخاب کنید."
    )

    buttons = []

    for professor in page_professors:
        course = (
            professor["course"]
            or "بدون درس"
        )

        button_text = (
            f"👤 {professor['name'][:20]} | "
            f"{course[:15]}"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    button_text,
                    callback_data=(
                        f"admin_prof:{professor['id']}"
                    ),
                )
            ]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️ قبلی",
                callback_data=(
                    f"manage_professors:{page - 1}"
                ),
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "بعدی ▶️",
                callback_data=(
                    f"manage_professors:{page + 1}"
                ),
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                "🔙 پنل مدیریت",
                callback_data="admin_panel",
            )
        ]
    )

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


async def admin_professor_details(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await q.answer()

    try:
        professor_id = int(
            q.data.split(":", 1)[1]
        )
    except (IndexError, ValueError):
        await q.edit_message_text(
            "❌ اطلاعات استاد نامعتبر است.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    professor = get_professor_by_id(
        professor_id
    )

    if not professor:
        await q.edit_message_text(
            "❌ استاد پیدا نشد.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    info = get_rating_info(
        professor_id
    )

    text = (
        "👤 <b>اطلاعات استاد</b>\n\n"
        f"نام: <b>{escape(professor['name'])}</b>\n"
        f"درس: <b>{escape(professor['course'] or 'ثبت نشده')}</b>\n"
        f"کد: <code>{escape(professor['code'])}</code>\n\n"
        f"میانگین: <b>{info['average']} از 5</b>\n"
        f"تعداد رأی: <b>{info['total']}</b>"
    )

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑 حذف استاد",
                        callback_data=(
                            f"delete_prof:{professor_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 بازگشت",
                        callback_data="manage_professors:0",
                    )
                ],
            ]
        ),
    )


# ============================================================
# DELETE PROFESSOR
# ============================================================

async def delete_professor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await q.answer()

    try:
        professor_id = int(
            q.data.split(":", 1)[1]
        )
    except (IndexError, ValueError):
        await q.edit_message_text(
            "❌ اطلاعات استاد نامعتبر است.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    professor = get_professor_by_id(
        professor_id
    )

    if not professor:
        await q.edit_message_text(
            "❌ استاد پیدا نشد.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    await q.edit_message_text(
        (
            "⚠️ <b>تأیید حذف</b>\n\n"
            f"آیا «<b>{escape(professor['name'])}</b>» "
            "حذف شود؟\n\n"
            "تمام امتیازها و نظرات این استاد نیز "
            "حذف خواهند شد."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚠️ بله، حذف شود",
                        callback_data=(
                            f"confirm_delete:{professor_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data=(
                            f"admin_prof:{professor_id}"
                        ),
                    )
                ],
            ]
        ),
    )


async def confirm_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    if not is_admin(q.from_user.id):
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await q.answer()

    try:
        professor_id = int(
            q.data.split(":", 1)[1]
        )
    except (IndexError, ValueError):
        await q.edit_message_text(
            "❌ اطلاعات استاد نامعتبر است.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    success = delete_professor(
        professor_id
    )

    if success:
        await q.edit_message_text(
            "✅ استاد با موفقیت حذف شد.",
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await q.edit_message_text(
            "❌ استاد پیدا نشد یا قبلاً حذف شده است.",
            reply_markup=admin_menu_keyboard(),
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    was_admin_flow = bool(
        context.user_data.get(
            "admin_flow"
        )
    )

    context.user_data.clear()

    if update.message:
        if was_admin_flow and is_admin(user_id):
            await update.message.reply_text(
                "❌ عملیات لغو شد.",
                reply_markup=admin_menu_keyboard(),
            )
        else:
            await update.message.reply_text(
                "❌ عملیات لغو شد.",
                reply_markup=main_menu_keyboard(
                    user_id
                ),
            )

    return ConversationHandler.END


async def cancel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query

    user_id = q.from_user.id

    was_admin_flow = bool(
        context.user_data.get(
            "admin_flow"
        )
    )

    await q.answer()

    context.user_data.clear()

    if was_admin_flow and is_admin(user_id):
        await q.edit_message_text(
            "🔐 <b>پنل مدیریت</b>\n\n"
            "یک گزینه را انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await q.edit_message_text(
            "❌ عملیات لغو شد.",
            reply_markup=main_menu_keyboard(
                user_id
            ),
        )

    return ConversationHandler.END


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled Telegram error.",
        exc_info=context.error,
    )


# ============================================================
# BUILD TELEGRAM APPLICATION
# ============================================================

def build_telegram_application():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty. "
            "No user will have admin access."
        )

    init_database()

    telegram_app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # STUDENT ADD
    # --------------------------------------------------------

    student_add = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                student_add_start,
                pattern=r"^student_add$",
            )
        ],
        states={
            ADD_NAME: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    student_receive_name,
                )
            ],
            ADD_COURSE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    student_receive_course,
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # --------------------------------------------------------
    # PROFESSOR CODE
    # --------------------------------------------------------

    code_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                enter_code_start,
                pattern=r"^enter_code$",
            )
        ],
        states={
            PROFESSOR_CODE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_code,
                )
            ]
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # --------------------------------------------------------
    # RATING
    # --------------------------------------------------------

    rating_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                rating_start,
                pattern=r"^rate:\d+$",
            )
        ],
        states={
            RATING_SCORE: [
                CallbackQueryHandler(
                    select_score,
                    pattern=r"^score:[1-5]$",
                ),
                CallbackQueryHandler(
                    cancel_callback,
                    pattern=r"^cancel_rating$",
                ),
            ],
            RATING_COMMENT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_comment,
                ),
                CallbackQueryHandler(
                    skip_comment,
                    pattern=r"^skip_comment$",
                ),
                CallbackQueryHandler(
                    cancel_callback,
                    pattern=r"^cancel_rating$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # --------------------------------------------------------
    # ADMIN ADD
    # --------------------------------------------------------

    admin_add = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_add_start,
                pattern=r"^admin_add$",
            )
        ],
        states={
            ADMIN_ADD_NAME: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    admin_receive_name,
                )
            ],
            ADMIN_ADD_COURSE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    admin_receive_course,
                )
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # --------------------------------------------------------
    # CONVERSATIONS FIRST
    # --------------------------------------------------------

    telegram_app.add_handler(
        student_add
    )

    telegram_app.add_handler(
        code_conv
    )

    telegram_app.add_handler(
        rating_conv
    )

    telegram_app.add_handler(
        admin_add
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        )
    )

    # --------------------------------------------------------
    # GENERAL CALLBACKS
    # --------------------------------------------------------

    telegram_app.add_handler(
        CallbackQueryHandler(
            main_menu,
            pattern=r"^main_menu$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            list_professors,
            pattern=r"^list_professors(?::\d+)?$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            view_professor,
            pattern=r"^view_prof:\d+(?::\d+)?$",
        )
    )

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    telegram_app.add_handler(
        CallbackQueryHandler(
            admin_panel,
            pattern=r"^admin_panel$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            pending_requests,
            pattern=r"^pending_requests(?::\d+)?$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            manage_professors,
            pattern=r"^manage_professors(?::\d+)?$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            admin_professor_details,
            pattern=r"^admin_prof:\d+$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            delete_professor,
            pattern=r"^delete_prof:\d+$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            confirm_delete,
            pattern=r"^confirm_delete:\d+$",
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            request_action,
            pattern=r"^(approve|reject):\d+$",
        )
    )

    # --------------------------------------------------------
    # ERROR HANDLER
    # --------------------------------------------------------

    telegram_app.add_error_handler(
        error_handler
    )

    return telegram_app


# ============================================================
# RUN BOT (FIXED FOR PYTHON 3.14+)
# ============================================================

async def run_bot_async():
    """Async version of run_bot."""
    try:
        telegram_app = build_telegram_application()

        logger.info("🤖 Telegram Ostad Bot is starting...")
        logger.info("BOT_TOKEN loaded: %s", bool(BOT_TOKEN))
        logger.info("ADMIN_IDS loaded: %s", ADMIN_IDS)
        logger.info("📚 Database: %s", DATABASE_PATH)
        logger.info("💬 Maximum comment words: %s", MAX_COMMENT_WORDS)
        logger.info("📋 Professor page size: %s", PROFESSOR_PAGE_SIZE)
        logger.info("💬 Comment page size: %s", COMMENT_PAGE_SIZE)

        # ✅ راه‌حل اصلی: استفاده از initialize و start به صورت دستی
        await telegram_app.initialize()
        await telegram_app.start()
        
        # شروع polling
        await telegram_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        logger.info("✅ Telegram bot is running and polling...")

        # نگه داشتن برنامه در حالت اجرا
        try:
            while True:
                await asyncio.sleep(3600)  # هر ساعت یک بار بیدار می‌شود
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Received shutdown signal...")
        finally:
            logger.info("🛑 Stopping Telegram bot...")
            await telegram_app.stop()
            await telegram_app.shutdown()
            logger.info("✅ Telegram bot stopped.")

    except Exception:
        logger.exception("❌ Telegram bot stopped because of an error.")
        raise


def run_bot():
    """Run the bot with proper event loop handling."""
    try:
        # ایجاد یک حلقه رویداد جدید
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # اجرای تابع async در حلقه
            loop.run_until_complete(run_bot_async())
        finally:
            loop.close()
            
    except Exception:
        logger.exception("❌ Failed to run Telegram bot.")
        raise


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():
    return "✅ Telegram Ostad Bot is running!"


@app.route("/health")
def health():
    return "OK"


@app.route("/ping")
def ping():
    return "pong"


def run_web():
    port = int(
        os.environ.get(
            "PORT",
            "5000",
        )
    )

    logger.info(
        "🌐 Flask server starting on port %s",
        port,
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    logger.info("🚀 Starting Ostad Bot service...")

    # Flask در ترد بک‌گراند اجرا می‌شود
    web_thread = threading.Thread(
        target=run_web,
        name="flask-web",
        daemon=True,
    )

    web_thread.start()

    # Telegram در ترد اصلی اجرا می‌شود (برای جلوگیری از خطای asyncio)
    run_bot()
