#!/usr/bin/env python3
"""
Telegram-бот для Ивана Гуничева — инструктора по вождению.

Расписание авто-уведомлений:
  07:00 — сводка записей на сегодня
  19:00 — напоминание о записях на завтра

Команды:
  /today      — записи на сегодня
  /tomorrow   — записи на завтра
  /week       — записи на 7 дней (полные карточки)
  /bookings   — все активные записи
  /id 42      — подробности конкретной записи
"""
import logging
import os
import sys
import functools
from datetime import date, time, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

# ── Подключаем Django ORM ──────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "driving_instructor.settings")

import django
django.setup()

from django.conf import settings
from core.models import Booking
from asgiref.sync import sync_to_async

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TZ    = ZoneInfo(settings.TIME_ZONE)
TOKEN = settings.TELEGRAM_BOT_TOKEN
try:
    CHAT = int(settings.TELEGRAM_CHAT_ID)
except (ValueError, TypeError):
    CHAT = None

DAY_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAY_RU_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ── Запросы к БД (sync → async) ────────────────────────────────────────────────

@sync_to_async
def _get_bookings_for_date(d: date) -> list:
    return list(
        Booking.objects
        .filter(status=Booking.STATUS_ACTIVE, slot__date=d)
        .select_related("slot")
        .order_by("slot__start_time")
    )


@sync_to_async
def _get_bookings_week(start: date) -> list:
    return list(
        Booking.objects
        .filter(
            status=Booking.STATUS_ACTIVE,
            slot__date__gte=start,
            slot__date__lt=start + timedelta(days=7),
        )
        .select_related("slot")
        .order_by("slot__date", "slot__start_time")
    )


@sync_to_async
def _get_all_active_bookings() -> list:
    return list(
        Booking.objects
        .filter(status=Booking.STATUS_ACTIVE)
        .select_related("slot")
        .order_by("slot__date", "slot__start_time")
    )


@sync_to_async
def _get_booking_by_id(pk: int):
    return Booking.objects.select_related("slot").get(pk=pk)


# ── Авторизация ────────────────────────────────────────────────────────────────

def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT:
            return
        return await func(update, ctx)
    return wrapper


# ── Форматирование ─────────────────────────────────────────────────────────────

def booking_card(b: Booking, compact: bool = False) -> str:
    d = b.get_display_date
    date_str = d.strftime("%d.%m.%Y") if d else "—"
    time_str = b.get_display_time or "—"
    dow      = DAY_RU[d.weekday()] if d else ""

    if compact:
        line = f"⏰ <b>{time_str}</b>  —  {b.name}   <code>{b.phone}</code>"
        if b.comment:
            short = b.comment[:70] + ("…" if len(b.comment) > 70 else "")
            line += f"\n   💬 <i>{short}</i>"
        return line

    lines = [
        f"<b>📋 Запись #{b.id}</b>",
        f"📅 {dow}, {date_str}   ⏰ {time_str}",
        f"👤 {b.name}",
        f"📞 <code>{b.phone}</code>",
    ]
    if b.comment:
        lines.append(f"💬 {b.comment}")
    if b.owner_note:
        lines.append(f"📝 <i>{b.owner_note}</i>")
    return "\n".join(lines)


def day_digest(bookings: list, header: str, compact: bool = True) -> str:
    if not bookings:
        return f"{header}\n\nЗаписей нет."
    lines = [header, ""]
    for b in bookings:
        lines.append(booking_card(b, compact=compact))
    n = len(bookings)
    lines.append(f"\n<i>Итого: {n} {'запись' if n == 1 else 'записи' if n < 5 else 'записей'}</i>")
    return "\n".join(lines)


def week_digest(bookings: list) -> list[str]:
    if not bookings:
        return ["На ближайшие 7 дней записей нет."]

    by_date: dict = defaultdict(list)
    for b in bookings:
        by_date[b.slot.date].append(b)

    messages = []
    current = []
    total = sum(len(v) for v in by_date.values())

    header_line = (
        f"📆 <b>Расписание на 7 дней</b>\n"
        f"<i>Записей: {total}</i>"
    )
    current.append(header_line)

    for d in sorted(by_date):
        dow  = DAY_RU[d.weekday()]
        date_header = f"\n{'─' * 20}\n🗓 <b>{dow}, {d.strftime('%d.%m.%Y')}</b>"
        day_lines = [date_header]

        for b in by_date[d]:
            t = b.get_display_time or "—"
            card_lines = [
                f"\n⏰ <b>{t}</b>",
                f"👤 {b.name}",
                f"📞 <code>{b.phone}</code>",
            ]
            if b.comment:
                card_lines.append(f"💬 <i>{b.comment}</i>")
            day_lines.append("\n".join(card_lines))

        block = "\n".join(day_lines)

        if len("\n".join(current)) + len(block) > 3800:
            messages.append("\n".join(current))
            current = [block]
        else:
            current.append(block)

    if current:
        messages.append("\n".join(current))

    return messages


# ── Команды ────────────────────────────────────────────────────────────────────

async def cmd_mychatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твой chat_id: <code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Привет, Иван!</b>\n\n"
        "Я слежу за твоим расписанием.\n\n"
        "<b>Команды:</b>\n"
        "/today      — записи на сегодня\n"
        "/tomorrow   — записи на завтра\n"
        "/week       — записи на 7 дней\n"
        "/bookings   — все активные записи\n"
        "/id 42      — подробности записи #42\n\n"
        "<i>Авто: в 07:00 — сводка на сегодня\n"
        "в 19:00 — напоминание на завтра</i>",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    bookings = await _get_bookings_for_date(today)
    text = day_digest(bookings, f"📅 <b>Сегодня — {DAY_RU[today.weekday()]}, {today.strftime('%d.%m.%Y')}</b>")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@owner_only
async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tomorrow = date.today() + timedelta(days=1)
    bookings = await _get_bookings_for_date(tomorrow)
    text = day_digest(bookings, f"📅 <b>Завтра — {DAY_RU[tomorrow.weekday()]}, {tomorrow.strftime('%d.%m.%Y')}</b>")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@owner_only
async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bookings = await _get_bookings_week(date.today())
    for msg in week_digest(bookings):
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@owner_only
async def cmd_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bookings = await _get_all_active_bookings()
    if not bookings:
        await update.message.reply_text("Активных записей нет.")
        return

    lines = [f"📋 <b>Все активные записи ({len(bookings)})</b>\n"]
    for b in bookings:
        d = b.get_display_date
        ds = d.strftime("%d.%m") if d else "—"
        dow = DAY_RU_SHORT[d.weekday()] if d else ""
        lines.append(
            f"<code>#{b.id}</code>  {ds} {dow}  {b.get_display_time or '—'}"
            f"  —  <b>{b.name}</b>  <code>{b.phone}</code>"
        )
    lines.append("\n<i>/id &lt;номер&gt; — полная карточка</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@owner_only
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Укажи номер записи: /id 42")
        return
    try:
        b = await _get_booking_by_id(int(ctx.args[0]))
        await update.message.reply_text(booking_card(b, compact=False), parse_mode=ParseMode.HTML)
    except (ValueError, Booking.DoesNotExist):
        await update.message.reply_text("Запись не найдена.")


# ── Scheduled jobs ─────────────────────────────────────────────────────────────

async def job_morning(ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    bookings = await _get_bookings_for_date(today)
    text = day_digest(
        bookings,
        f"☀️ <b>Доброе утро, Иван!</b>\n"
        f"<b>Сегодня — {DAY_RU[today.weekday()]}, {today.strftime('%d.%m.%Y')}</b>",
    )
    await ctx.bot.send_message(chat_id=CHAT, text=text, parse_mode=ParseMode.HTML)


async def job_tomorrow_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    tomorrow = date.today() + timedelta(days=1)
    bookings = await _get_bookings_for_date(tomorrow)
    if not bookings:
        return
    text = day_digest(
        bookings,
        f"🔔 <b>Завтра — {DAY_RU[tomorrow.weekday()]}, {tomorrow.strftime('%d.%m.%Y')}</b>",
    )
    await ctx.bot.send_message(chat_id=CHAT, text=text, parse_mode=ParseMode.HTML)


# ── Меню команд бота ──────────────────────────────────────────────────────────

async def set_bot_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("today",    "Записи на сегодня"),
        BotCommand("tomorrow", "Записи на завтра"),
        BotCommand("week",     "Расписание на 7 дней"),
        BotCommand("bookings", "Все активные записи"),
        BotCommand("id",       "Карточка записи: /id 42"),
    ])


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан в .env")
        return
    if not CHAT:
        logger.error("TELEGRAM_CHAT_ID не задан в .env")
        return

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(set_bot_commands)
        .build()
    )

    app.add_handler(CommandHandler("mychatid", cmd_mychatid))
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("today",    cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week",     cmd_week))
    app.add_handler(CommandHandler("bookings", cmd_bookings))
    app.add_handler(CommandHandler("id",       cmd_id))

    jq = app.job_queue
    jq.run_daily(job_morning,           time=time(7,  0, tzinfo=TZ))
    jq.run_daily(job_tomorrow_reminder, time=time(19, 0, tzinfo=TZ))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
