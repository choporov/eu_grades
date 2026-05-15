from __future__ import annotations

import logging
from datetime import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings, load_settings
from .dates import current_week_bounds, date_range_filter, previous_week_bounds, today_in
from .drive import DriveWorkbookProvider, LocalWorkbookProvider
from .formatting import (
    format_entries,
    format_period_entries_by_date,
    format_subjects,
    format_today_entries,
    split_telegram_message,
)
from .grades import GradesRepository, is_valid_email, normalize_email
from .storage import UserStorage


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


HELP_TEXT = """Команди:
/start - авторизація email
/stop - припинити діалог і видалити email
/email student@example.com - змінити email
/refresh - перечитати таблиці з Drive/папки
/subjects - перелік дисциплін
/grades - усі оцінки та пропуски
/grades today - за сьогодні
/grades week - за поточний тиждень
/grades lastweek - за попередній тиждень
/grades назва дисципліни - з конкретної дисципліни
/today - за сьогодні
/week - за поточний тиждень
/lastweek - за попередній тиждень
/subject назва дисципліни - з конкретної дисципліни"""


REPORT_MENU_TEXT = "Оберіть звіт:"
REPORT_CALLBACK_PREFIX = "report:"


class BotServices:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = UserStorage(settings.data_dir / "users.json")
        self.repository = GradesRepository(
            provider=create_provider(settings),
            cache_ttl_seconds=settings.cache_ttl_seconds,
        )


def create_provider(settings: Settings):
    if settings.grades_source == "local":
        return LocalWorkbookProvider(
            settings.local_grades_dir,
            credentials_file=settings.google_credentials_file,
            cache_dir=settings.data_dir / "local_gsheet_cache",
        )
    if not settings.drive_folder_id:
        raise ValueError("GRADES_DRIVE_FOLDER_ID is required for GRADES_SOURCE=drive.")
    if not settings.google_credentials_file:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS is required for GRADES_SOURCE=drive.")
    return DriveWorkbookProvider(
        folder_id=settings.drive_folder_id,
        credentials_file=settings.google_credentials_file,
        cache_dir=settings.data_dir / "drive_cache",
    )


def report_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("/grades", callback_data=f"{REPORT_CALLBACK_PREFIX}grades")],
            [
                InlineKeyboardButton("/today", callback_data=f"{REPORT_CALLBACK_PREFIX}today"),
                InlineKeyboardButton("/week", callback_data=f"{REPORT_CALLBACK_PREFIX}week"),
            ],
        ]
    )


def main() -> None:
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required.")

    services = BotServices(settings)
    application = build_application(settings, services)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def build_application(settings: Settings, services: BotServices) -> Application:
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.bot_data["services"] = services

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("email", email_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("subjects", subjects_command))
    application.add_handler(CommandHandler("grades", grades_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("week", week_command))
    application.add_handler(CommandHandler("lastweek", lastweek_command))
    application.add_handler(CommandHandler("subject", subject_command))
    application.add_handler(CallbackQueryHandler(report_menu_callback, pattern=f"^{REPORT_CALLBACK_PREFIX}"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, email_message))

    schedule_jobs(application, settings)
    return application


def schedule_jobs(application: Application, settings: Settings) -> None:
    if application.job_queue is None:
        logger.warning("Job queue is unavailable. Install python-telegram-bot[job-queue].")
        return

    application.job_queue.run_daily(
        daily_summary_job,
        time=time(hour=19, minute=0, tzinfo=settings.timezone),
        days=(1, 2, 3, 4, 5),
        name="daily-summary",
    )
    application.job_queue.run_daily(
        weekly_summary_job,
        time=time(hour=10, minute=0, tzinfo=settings.timezone),
        days=(0,),
        name="weekly-summary",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    chat_id = require_chat_id(update)
    user = services.storage.get(chat_id)
    if user is None:
        await update.message.reply_text("Введіть адресу електронної пошти для авторизації.")
        return

    grades = get_student_grades_for_request(services, user.email)
    message = f"Ви авторизовані як {user.email}.\n\n{format_subjects(grades.disciplines)}"
    await update.message.reply_text(message, reply_markup=report_menu_keyboard())


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    chat_id = require_chat_id(update)
    services.storage.delete(chat_id)
    await update.message.reply_text(
        "Діалог припинено. Ваш email видалено з бота. Щоб знову користуватися ботом, надішліть /start."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"{HELP_TEXT}\n\n{REPORT_MENU_TEXT}", reply_markup=report_menu_keyboard())


async def email_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not is_valid_email(text):
        await update.message.reply_text("Не схоже на email. Надішліть адресу у форматі student@example.com.")
        return
    await authorize_email(update, context, text)


async def email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Вкажіть email: /email student@example.com")
        return
    email = " ".join(context.args).strip()
    if not is_valid_email(email):
        await update.message.reply_text("Не схоже на email. Перевірте адресу та спробуйте ще раз.")
        return
    await authorize_email(update, context, email)


async def authorize_email(update: Update, context: ContextTypes.DEFAULT_TYPE, email: str) -> None:
    services = get_services(context)
    chat_id = require_chat_id(update)
    normalized_email = normalize_email(email)
    services.storage.set_email(chat_id, normalized_email)
    grades = get_student_grades_for_request(services, normalized_email)
    await update.message.reply_text(format_subjects(grades.disciplines), reply_markup=report_menu_keyboard())


async def subjects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user is None:
        return
    services = get_services(context)
    grades = get_student_grades_for_request(services, user.email)
    await update.message.reply_text(format_subjects(grades.disciplines))


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user is None:
        return
    services = get_services(context)
    grades = services.repository.get_student_grades(user.email, force_reload=True)
    await update.message.reply_text(format_subjects(grades.disciplines), reply_markup=report_menu_keyboard())


async def report_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    services = get_services(context)
    chat_id = query.message.chat_id if query.message else require_chat_id(update)
    user = services.storage.get(chat_id)
    if user is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Спочатку авторизуйтесь: надішліть свою email-адресу.",
        )
        return

    action = query.data.removeprefix(REPORT_CALLBACK_PREFIX) if query.data else ""
    if action == "grades":
        await send_all_grades_report_to_chat(context, chat_id, user.email)
    elif action == "today":
        await send_period_entries_to_chat(context, chat_id, user.email, "today")
    elif action == "week":
        await send_period_entries_to_chat(context, chat_id, user.email, "week")


async def grades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user is None:
        return

    args = " ".join(context.args).strip()
    if not args:
        await send_all_grades_report(update, context, user.email)
        return

    key = args.casefold()
    if key in {"today", "сьогодні", "день"}:
        await send_period_entries(update, context, user.email, "today")
    elif key in {"week", "тиждень", "цей тиждень"}:
        await send_period_entries(update, context, user.email, "week")
    elif key in {"lastweek", "last week", "минулий тиждень", "попередній тиждень"}:
        await send_period_entries(update, context, user.email, "lastweek")
    else:
        await send_subject_entries(update, context, user.email, args)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user:
        await send_period_entries(update, context, user.email, "today")


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user:
        await send_period_entries(update, context, user.email, "week")


async def lastweek_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user:
        await send_period_entries(update, context, user.email, "lastweek")


async def subject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_authorized_user(update, context)
    if user is None:
        return
    subject_query = " ".join(context.args).strip()
    if not subject_query:
        await update.message.reply_text("Вкажіть назву або частину назви дисципліни: /subject Бази даних")
        return
    await send_subject_entries(update, context, user.email, subject_query)


async def send_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    email: str,
    empty_text: str,
    start=None,
    end=None,
    subject_query: str | None = None,
) -> None:
    services = get_services(context)
    grades = get_student_grades_for_request(services, email)
    entries = list(date_range_filter(list(grades.entries), start, end))

    if subject_query:
        query = subject_query.casefold()
        entries = [entry for entry in entries if query in entry.discipline.casefold()]
        if not entries:
            empty_text = f"Записів з дисципліни «{subject_query}» не знайдено."

    text = format_entries(entries, empty_text=empty_text)
    for chunk in split_telegram_message(text):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def send_all_grades_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    email: str,
) -> None:
    await send_all_grades_report_to_chat(context, require_chat_id(update), email)


async def send_all_grades_report_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    email: str,
) -> None:
    services = get_services(context)
    grades = get_student_grades_for_request(services, email)
    text = format_period_entries_by_date(
        list(grades.entries),
        empty_text="Оцінок і пропусків не знайдено.",
        include_discipline_averages=True,
    )
    for chunk in split_telegram_message(text):
        await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML)


async def send_period_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    email: str,
    period: str,
) -> None:
    await send_period_entries_to_chat(context, require_chat_id(update), email, period)


async def send_period_entries_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    email: str,
    period: str,
) -> None:
    services = get_services(context)
    today = today_in(services.settings.timezone)
    if period == "today":
        grades = get_student_grades_for_request(services, email)
        entries = list(date_range_filter(list(grades.entries), today, today))
        text = format_today_entries(entries, grades.disciplines, today)
        for chunk in split_telegram_message(text):
            await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML)
    elif period == "week":
        start, end = current_week_bounds(today)
        await send_date_grouped_period_entries_to_chat(
            context,
            chat_id,
            email,
            start,
            end,
            empty_text="За поточний тиждень записів не знайдено.",
        )
    elif period == "lastweek":
        start, end = previous_week_bounds(today)
        await send_date_grouped_period_entries_to_chat(
            context,
            chat_id,
            email,
            start,
            end,
            empty_text="За попередній тиждень записів не знайдено.",
        )


async def send_date_grouped_period_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    email: str,
    start,
    end,
    empty_text: str,
) -> None:
    await send_date_grouped_period_entries_to_chat(
        context,
        require_chat_id(update),
        email,
        start,
        end,
        empty_text,
    )


async def send_date_grouped_period_entries_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    email: str,
    start,
    end,
    empty_text: str,
) -> None:
    services = get_services(context)
    grades = get_student_grades_for_request(services, email)
    entries = list(date_range_filter(list(grades.entries), start, end))
    text = format_period_entries_by_date(entries, empty_text=empty_text)
    for chunk in split_telegram_message(text):
        await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML)


async def send_subject_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    email: str,
    subject_query: str,
) -> None:
    await send_entries(
        update,
        context,
        email,
        empty_text=f"Записів з дисципліни «{subject_query}» не знайдено.",
        subject_query=subject_query,
    )


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    today = today_in(services.settings.timezone)
    await send_scheduled_daily_summary(context, today)


async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    today = today_in(services.settings.timezone)
    start, end = current_week_bounds(today)
    await send_scheduled_summary(
        context,
        start=start,
        end=end,
        empty_text="За поточний тиждень записів не знайдено.",
    )


async def send_scheduled_summary(
    context: ContextTypes.DEFAULT_TYPE,
    start,
    end,
    empty_text: str,
) -> None:
    services = get_services(context)
    reload_repository_for_scheduled_run(services)
    for user in services.storage.all_users():
        grades = services.repository.get_student_grades(user.email)
        entries = date_range_filter(list(grades.entries), start, end)
        if not entries and not services.settings.send_empty_summaries:
            continue
        text = format_period_entries_by_date(entries, empty_text=empty_text)
        for chunk in split_telegram_message(text):
            try:
                await context.bot.send_message(
                    chat_id=user.chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Failed to send scheduled summary to chat_id=%s", user.chat_id)


async def send_scheduled_daily_summary(context: ContextTypes.DEFAULT_TYPE, today) -> None:
    services = get_services(context)
    reload_repository_for_scheduled_run(services)
    for user in services.storage.all_users():
        grades = services.repository.get_student_grades(user.email)
        entries = date_range_filter(list(grades.entries), today, today)
        if not entries and not services.settings.send_empty_summaries:
            continue
        text = format_today_entries(entries, grades.disciplines, today)
        for chunk in split_telegram_message(text):
            try:
                await context.bot.send_message(
                    chat_id=user.chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Failed to send scheduled daily summary to chat_id=%s", user.chat_id)


async def require_authorized_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = get_services(context)
    chat_id = require_chat_id(update)
    user = services.storage.get(chat_id)
    if user is None:
        await update.message.reply_text("Спочатку авторизуйтесь: надішліть свою email-адресу.")
    return user


def require_chat_id(update: Update) -> int:
    if update.effective_chat is None:
        raise RuntimeError("Update has no chat.")
    return update.effective_chat.id


def get_services(context: ContextTypes.DEFAULT_TYPE) -> BotServices:
    return context.application.bot_data["services"]


def get_student_grades_for_request(services: BotServices, email: str):
    return services.repository.get_student_grades(
        email,
        force_reload=services.settings.grades_source == "drive",
    )


def reload_repository_for_scheduled_run(services: BotServices) -> None:
    if services.settings.grades_source == "drive":
        services.repository.reload()
