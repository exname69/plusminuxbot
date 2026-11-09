"""A minimal work income/expense Telegram bot."""
import asyncio
from datetime import datetime, time, timedelta
import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from .parser import ParseError, Entry, parse_message
from .storage import Storage, period_bounds
from .sheets_backup import SheetsBackup

router = Router()
storage: Storage
currency: str
sheets_backup: SheetsBackup | None = None
admin_id: int | None = None
backup_hour: int = 3
backup_lock = asyncio.Lock()

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="📅 Неделя")],
        [KeyboardButton(text="💰 Месяц"), KeyboardButton(text="📋 История")],
    ],
    resize_keyboard=True,
    input_field_placeholder="+100 или -50 аренда",
)

MENU_ADMIN = ReplyKeyboardMarkup(
    keyboard=[*MENU.keyboard, [KeyboardButton(text="📤 Выгрузить")]],
    resize_keyboard=True,
    input_field_placeholder="+100 или -50 аренда",
)


def menu_for(user_id: int) -> ReplyKeyboardMarkup:
    return MENU_ADMIN if admin_id is not None and user_id == admin_id and sheets_backup else MENU


class EditTransaction(StatesGroup):
    waiting_for_new_text = State()


def format_money(amount_minor: int, *, explicit_plus: bool = False) -> str:
    sign = "−" if amount_minor < 0 else "+" if explicit_plus and amount_minor > 0 else ""
    abs_amount = abs(amount_minor)
    major, minor = divmod(abs_amount, 100)
    amount_str = f"{major:,}".replace(",", " ")
    if minor:
        amount_str += f",{minor:02d}"
    return f"{sign}{amount_str}{(' ' + currency) if currency else ''}"


def entry_summary(entry: Entry) -> str:
    return f"{format_money(entry.amount_minor, explicit_plus=True)}{(' · ' + entry.comment) if entry.comment else ''}"


def operation_buttons(transaction_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{transaction_id}"),
            InlineKeyboardButton(text="↩️ Отменить", callback_data=f"undo:{transaction_id}"),
        ]
    ])


HELP = (
    "💼 plusminuxbot — учёт рабочих денег\n\n"
    "+100 — приход\n"
    "-50 — расход\n"
    "+240 ногти — приход с комментарием\n"
    "-300 материалы — расход с комментарием\n"
    "вчера -800 аренда — запись за вчера\n\n"
    "Можно отправить несколько операций, каждую с новой строки.\n"
    "Комментарий необязателен. Категории указывать не нужно.\n\n"
    "📊 Сегодня · 📅 Неделя · 💰 Месяц · 📋 История\n"
    "/undo — отменить последнюю запись\n"
    "/balance — итог за всё время\n"
    "/backup — выгрузить копию в Google Sheets (только владелец)\n"
    "/id — узнать свой Telegram ID\n"
    "/cancel — выйти из изменения записи"
)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Привет! 👋\n\n" + HELP, reply_markup=menu_for(message.from_user.id))


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP, reply_markup=menu_for(message.from_user.id))


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Изменение отменено.", reply_markup=menu_for(message.from_user.id))


@router.message(Command("undo"))
async def undo_command(message: Message) -> None:
    row = storage.undo_last(message.from_user.id)
    if row is None:
        await message.answer("Пока нечего отменять.")
    else:
        await message.answer(f"↩️ Удалено: {format_money(row['amount_minor'], explicit_plus=True)}")


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


async def export_sheets():
    if sheets_backup is None:
        raise RuntimeError("Google Sheets не настроен")
    async with backup_lock:
        return await asyncio.to_thread(sheets_backup.export, storage)


@router.message(Command("backup"))
@router.message(F.text == "📤 Выгрузить")
async def backup_now(message: Message) -> None:
    if admin_id is None or message.from_user.id != admin_id:
        await message.answer("⛔ Ручная выгрузка доступна только владельцу. Для настройки: /id")
        return
    if sheets_backup is None:
        await message.answer("⚠️ Google Sheets не настроен. Заполни GOOGLE_SHEET_ID в .env")
        return
    await message.answer("📤 Создаю резервную копию...")
    try:
        result = await export_sheets()
        now = datetime.now(storage.tz)
        if now.hour >= backup_hour:
            storage.set_last_backup_day(now.date().isoformat())
    except Exception:
        logging.exception("Manual Sheets export failed")
        await message.answer("❌ Выгрузка не удалась. Проверь доступ Google и логи контейнера.")
        return
    await message.answer(
        f"✅ Скопировано операций: {result.count}\n"
        f"Снимок: {result.title}\n{result.url}"
    )


async def scheduled_backup_loop(bot: Bot) -> None:
    """Run at/after 03:00 local, retry failures, catch up after downtime."""
    failure_notification_day = None
    while True:
        now = datetime.now(storage.tz)
        day = now.date().isoformat()
        scheduled = datetime.combine(now.date(), time(backup_hour), storage.tz)
        if now >= scheduled and storage.get_last_backup_day() != day:
            try:
                result = await export_sheets()
                storage.set_last_backup_day(day)
                logging.info("Daily Sheets backup: %s, %s rows", result.title, result.count)
                failure_notification_day = None
            except Exception:
                logging.exception("Daily Sheets backup failed; will retry in 15 minutes")
                if admin_id and failure_notification_day != day:
                    failure_notification_day = day
                    try:
                        await bot.send_message(
                            admin_id, "⚠️ Резервное копирование Google Sheets не удалось. "
                                      "Проверь логи: docker compose logs bot"
                        )
                    except Exception:
                        logging.exception("Could not notify admin about backup failure")
                await asyncio.sleep(15 * 60)
                continue
        now = datetime.now(storage.tz)
        next_run = datetime.combine(now.date(), time(backup_hour), storage.tz)
        if now >= next_run:
            next_run += timedelta(days=1)
        await asyncio.sleep(max(1, (next_run - now).total_seconds()))


async def send_report(message: Message, period: str) -> None:
    period_names = {"today": "Сегодня", "week": "Эта неделя", "month": "Этот месяц", "all": "За всё время"}
    start_ts, end_ts = period_bounds(period, storage.tz)
    totals = storage.period_totals(message.from_user.id, start_ts, end_ts)
    await message.answer(
        f"📊 {period_names[period]}\n\n"
        f"➕ Приход: {format_money(totals.income)}\n"
        f"➖ Расход: {format_money(totals.expenses)}\n"
        f"━━━━━━━━━━\n"
        f"💰 Итого: {format_money(totals.net, explicit_plus=True)}\n"
        f"Операций: {totals.count}"
    )


@router.message(Command("today"))
@router.message(F.text.in_({"📊 Сегодня", "сегодня"}))
async def today(message: Message) -> None:
    await send_report(message, "today")


@router.message(Command("week"))
@router.message(F.text.in_({"📅 Неделя", "неделя"}))
async def week(message: Message) -> None:
    await send_report(message, "week")


@router.message(Command("month"))
@router.message(F.text.in_({"💰 Месяц", "месяц"}))
async def month(message: Message) -> None:
    await send_report(message, "month")


@router.message(Command("balance"))
@router.message(F.text.in_({"остаток", "всё", "все"}))
async def balance(message: Message) -> None:
    await send_report(message, "all")


@router.message(Command("history"))
@router.message(F.text.in_({"📋 История", "история"}))
async def history(message: Message) -> None:
    rows = storage.recent(message.from_user.id)
    if not rows:
        await message.answer("Операций пока нет. Добавь первую: +100")
        return
    lines = ["📋 Последние 10 операций", ""]
    for row in rows:
        day = datetime.fromtimestamp(row["operation_ts"], storage.tz).strftime("%d.%m")
        comment = f" · {row['comment']}" if row["comment"] else ""
        lines.append(f"{day}  {format_money(row['amount_minor'], explicit_plus=True)}{comment}")
    await message.answer("\n".join(lines))


@router.message(F.text.in_({"отмена", "↩️ Отмена"}))
async def undo_text(message: Message) -> None:
    await undo_command(message)


@router.callback_query(F.data.startswith("undo:"))
async def undo_button(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    try:
        transaction_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная операция", show_alert=True)
        return
    if not storage.delete(callback.from_user.id, transaction_id):
        await callback.answer("Операция уже удалена или недоступна", show_alert=True)
        return
    await callback.answer("Операция удалена")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("↩️ Операция удалена")


@router.callback_query(F.data.startswith("edit:"))
async def edit_button(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    try:
        transaction_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная операция", show_alert=True)
        return
    if not storage.get(callback.from_user.id, transaction_id):
        await callback.answer("Операция уже удалена или недоступна", show_alert=True)
        return
    await state.set_state(EditTransaction.waiting_for_new_text)
    await state.update_data(transaction_id=transaction_id)
    await callback.answer()
    await callback.message.answer("✏️ Отправь исправленную запись, например:\n-250 материалы\n\n/cancel — отменить")


@router.message(EditTransaction.waiting_for_new_text)
async def edit_from_text(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Отправь текст операции: +100 или -50 аренда")
        return
    try:
        entries = parse_message(message.text)
        if len(entries) != 1:
            raise ParseError("Для изменения отправь одну операцию")
    except ParseError as exc:
        await message.answer(f"⚠️ {exc}\n/cancel — отменить")
        return
    data = await state.get_data()
    transaction_id = data.get("transaction_id")
    success = storage.edit(message.from_user.id, transaction_id, entries[0]) if transaction_id else False
    await state.clear()
    if not success:
        await message.answer("Операция не найдена (возможно, удалена).")
        return
    await message.answer("✅ Изменено: " + entry_summary(entries[0]), reply_markup=operation_buttons(transaction_id))


@router.message(F.text)
async def add_record(message: Message) -> None:
    try:
        entries = parse_message(message.text)
    except ParseError as exc:
        await message.answer(f"⚠️ {exc}\n\nПример: +100 или -50 аренда")
        return
    ids = storage.add_entries(message.from_user.id, message.chat.id, message.message_id, entries)
    if len(entries) == 1:
        await message.answer("✅ Записано: " + entry_summary(entries[0]), reply_markup=operation_buttons(ids[0]))
        return
    income = sum(e.amount_minor for e in entries if e.amount_minor > 0)
    expense = -sum(e.amount_minor for e in entries if e.amount_minor < 0)
    await message.answer(
        f"✅ Записано операций: {len(entries)}\n"
        f"➕ Приход: {format_money(income)}\n"
        f"➖ Расход: {format_money(expense)}\n"
        f"💰 Итого: {format_money(income-expense, explicit_plus=True)}\n\n"
        "Для отмены последней записи: /undo"
    )


async def main() -> None:
    global storage, currency, sheets_backup, admin_id, backup_hour
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "PASTE_BOTFATHER_TOKEN_HERE":
        raise SystemExit("Заполни BOT_TOKEN в .env (получи токен у @BotFather)")
    tz_str = os.getenv("BOT_TIMEZONE", "UTC")
    try:
        tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Неверный BOT_TIMEZONE: {tz_str}") from exc
    storage = Storage(os.getenv("DB_PATH", "data/finance.sqlite3"), tz)
    storage.initialize()
    currency = os.getenv("CURRENCY", "").strip()
    admin_text = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if admin_text:
        try:
            admin_id = int(admin_text)
            if admin_id <= 0:
                raise ValueError
        except ValueError as exc:
            raise SystemExit("ADMIN_TELEGRAM_ID должен быть положительным числом") from exc
    try:
        backup_hour = int(os.getenv("BACKUP_HOUR", "3"))
        retention = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))
        if not 0 <= backup_hour <= 23:
            raise ValueError
    except ValueError as exc:
        raise SystemExit("BACKUP_HOUR должен быть от 0 до 23, а BACKUP_RETENTION_DAYS числом") from exc
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if sheet_id:
        sheets_backup = SheetsBackup(
            sheet_id,
            os.getenv("GOOGLE_CREDENTIALS_FILE", "secrets/service-account.json"),
            tz,
            retention_days=retention,
        )

    bot = Bot(token=token)
    dispatcher = Dispatcher()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    dispatcher.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="today", description="Итоги за сегодня"),
        BotCommand(command="week", description="Итоги за неделю"),
        BotCommand(command="month", description="Итоги за месяц"),
        BotCommand(command="history", description="Последние операции"),
        BotCommand(command="undo", description="Отменить последнюю операцию"),
        BotCommand(command="balance", description="Итоги за всё время"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="id", description="Узнать свой Telegram ID"),
        BotCommand(command="backup", description="Выгрузить в Google Sheets"),
    ])
    logging.info("Bot started. Timezone=%s, DB=%s", tz_str, storage.path)
    backup_task = asyncio.create_task(scheduled_backup_loop(bot)) if sheets_backup else None
    if sheets_backup:
        logging.info("Google Sheets daily backup enabled at %02d:00", backup_hour)
    else:
        logging.info("Google Sheets not configured; SQLite continues without remote backup")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if backup_task:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
