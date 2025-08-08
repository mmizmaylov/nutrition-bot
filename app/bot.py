import asyncio
import base64
import logging
import os
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
import re

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler, filters

from app.db import get_session, init_db, get_or_create_user, set_user_calorie_target, set_user_timezone, get_today_totals
from app.db import add_meal
from app.db import get_all_users, get_meals_for_local_day, get_day_total_calories, has_summary_sent, mark_summary_sent
from app.vision_providers.openai_provider import analyze_meal
from app.prompt import build_system_prompt
from app.formatting import format_reply, format_daily_summary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")

GOAL_TO_PROPOSAL = {
    "lose": {
        "range_text": "Для похудения я рекомендую 1400–1600 ккал в день.",
        "proposed": 1500,
        "emoji_title": "🥗 Похудение",
    },
    "maintain": {
        "range_text": "Для поддержания веса рекомендую 1800–2200 ккал в день.",
        "proposed": 2000,
        "emoji_title": "⚖️ Поддержание веса",
    },
    "gain": {
        "range_text": "Для набора массы рекомендую 2300–2800 ккал в день.",
        "proposed": 2500,
        "emoji_title": "💪 Набор массы",
    },
}

COMMON_TZ = [
    ("Europe/Moscow", "Europe/Madrid"),
    ("Europe/Berlin", "Europe/London"),
    ("Asia/Almaty", "Europe/Istanbul"),
]


def goals_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(text="🥗 Похудение", callback_data="goal:lose"),
            InlineKeyboardButton(text="⚖️ Поддержание", callback_data="goal:maintain"),
            InlineKeyboardButton(text="💪 Набор массы", callback_data="goal:gain"),
        ]
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(text="Да", callback_data="confirm:yes"),
            InlineKeyboardButton(text="Изменить", callback_data="confirm:edit"),
        ]
    ])


def tz_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for left, right in COMMON_TZ:
        rows.append([
            InlineKeyboardButton(text=left, callback_data=f"tz:{left}"),
            InlineKeyboardButton(text=right, callback_data=f"tz:{right}"),
        ])
    rows.append([InlineKeyboardButton(text="Другой часовой пояс", callback_data="tz:other")])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with get_session() as session:
        user = get_or_create_user(session, user_id, DEFAULT_TZ)
        session.commit()
    greeting = (
        "👋 Привет!\n"
        "Я помогу тебе следить за питанием по фото 📸🍽️\n"
        "Сначала выбери свою цель:"
    )
    await update.message.reply_text(greeting, reply_markup=goals_keyboard())


# Daily summary scheduler
async def daily_summary_worker(app: Application) -> None:
    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            with get_session() as session:
                users = get_all_users(session)
                for user in users:
                    tz = ZoneInfo(user.timezone)
                    local_now = utc_now.astimezone(tz)
                    # Consider midnight window: 00:00:00 .. 00:10:00 local time
                    if not (local_now.hour == 0 and local_now.minute < 10):
                        continue

                    day_yesterday = (local_now.date() - timedelta(days=1))
                    day_str = day_yesterday.isoformat()

                    # Avoid duplicates
                    if has_summary_sent(session, user.telegram_id, day_str):
                        continue

                    meals = get_meals_for_local_day(session, user.telegram_id, day_yesterday, user.timezone)
                    items = [(m.dish, m.portion, m.calories) for m in meals]
                    total = get_day_total_calories(session, user.telegram_id, day_yesterday, user.timezone)
                    text = format_daily_summary(date_str=day_str, items=items, total_calories=total, target=user.calorie_target)

                    try:
                        await app.bot.send_message(chat_id=user.telegram_id, text=text)
                        mark_summary_sent(session, user.telegram_id, day_str)
                        session.commit()
                    except Exception:
                        logger.exception("Failed to send daily summary to %s", user.telegram_id)
        except Exception:
            logger.exception("daily_summary_worker iteration error")

        await asyncio.sleep(300)  # 5 minutes


async def handle_goal_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    goal = data.split(":", 1)[1] if ":" in data else ""
    cfg = GOAL_TO_PROPOSAL.get(goal)
    if not cfg:
        return

    proposed = cfg["proposed"]
    context.chat_data["proposed_calories"] = proposed
    context.chat_data["awaiting_manual_calories"] = False

    text = (
        f"Отлично! {cfg['range_text']}\n"
        f"Установим лимит {proposed} ккал?"
    )
    if query.message:
        await query.message.reply_text(text, reply_markup=confirm_keyboard())


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    action = data.split(":", 1)[1] if ":" in data else ""

    if action == "yes":
        proposed = context.chat_data.get("proposed_calories")
        if isinstance(proposed, int) and update.effective_user:
            with get_session() as session:
                get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
                set_user_calorie_target(session, update.effective_user.id, proposed)
                session.commit()
            msg = (
                f"✅ Готово! Твой дневной лимит: {proposed} ккал\n"
                "Теперь просто присылай мне фото еды — я скажу, что в тарелке, оценю калорийность и подскажу, сколько осталось до конца дня 💪"
            )
            if query.message:
                await query.message.reply_text(msg)
        context.chat_data.pop("proposed_calories", None)
        context.chat_data["awaiting_manual_calories"] = False

    elif action == "edit":
        context.chat_data["awaiting_manual_calories"] = True
        if query.message:
            await query.message.reply_text("Введи желаемый дневной лимит в ккал (целое число), например: 1800")


async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    text = (update.message.text or "").strip()

    # Manual calories entry (onboarding or /target)
    awaiting_onboarding = context.chat_data.get("awaiting_manual_calories") or context.user_data.get("awaiting_manual_calories")
    awaiting_set = context.chat_data.get("awaiting_setcalories") or context.user_data.get("awaiting_setcalories")
    if awaiting_onboarding or awaiting_set:
        m = re.search(r"-?\d+", text)
        if not m:
            await update.message.reply_text("Пожалуйста, введи положительное целое число, например: 1800")
            return
        try:
            target = int(m.group(0))
            if target <= 0:
                raise ValueError
        except Exception:
            await update.message.reply_text("Пожалуйста, введи положительное целое число, например: 1800")
            return

        with get_session() as session:
            get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
            set_user_calorie_target(session, update.effective_user.id, target)
            session.commit()

        context.chat_data["awaiting_manual_calories"] = False
        context.chat_data["awaiting_setcalories"] = False
        context.chat_data.pop("proposed_calories", None)
        context.user_data["awaiting_manual_calories"] = False
        context.user_data["awaiting_setcalories"] = False
        context.user_data.pop("proposed_calories", None)

        await update.message.reply_text(
            f"✅ Готово! Твой дневной лимит: {target} ккал\n"
            "Теперь просто присылай мне фото еды — я скажу, что в тарелке, оценю калорийность и подскажу, сколько осталось до конца дня 💪"
        )
        return

    # Manual timezone entry
    if context.chat_data.get("awaiting_timezone_manual") or context.user_data.get("awaiting_timezone_manual"):
        try:
            ZoneInfo(text)
        except Exception:
            await update.message.reply_text("Неизвестный TZ. Пример: Europe/Moscow, Europe/Berlin, Asia/Almaty")
            return
        with get_session() as session:
            get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
            set_user_timezone(session, update.effective_user.id, text)
            session.commit()
        context.chat_data["awaiting_timezone_manual"] = False
        context.user_data["awaiting_timezone_manual"] = False
        await update.message.reply_text(f"Часовой пояс обновлён: {text}")
        return

    # If none of the above matched and it's plain text, ignore silently
    return


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    if not context.args:
        context.chat_data["awaiting_setcalories"] = True
        await update.message.reply_text("Введи желаемый дневной лимит в ккал (целое число), например: 2000")
        return
    try:
        target = int(context.args[0])
        if target <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное целое число килокалорий.")
        return
    with get_session() as session:
        get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
        set_user_calorie_target(session, update.effective_user.id, target)
        session.commit()
    await update.message.reply_text(f"Дневной лимит установлен: {target} ккал")


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    # Direct argument mode: /timezone Europe/Paris
    if context.args:
        tzid = context.args[0]
        try:
            ZoneInfo(tzid)
        except Exception:
            await update.message.reply_text("Неизвестный TZ. Пример: Europe/Moscow, Europe/Berlin, Asia/Almaty")
            return
        with get_session() as session:
            get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
            set_user_timezone(session, update.effective_user.id, tzid)
            session.commit()
        await update.message.reply_text(f"Часовой пояс обновлён: {tzid}")
        return

    await update.message.reply_text(
        "Выбери часовой пояс или укажи другой:", reply_markup=tz_keyboard()
    )


async def handle_tz_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 1)
        if len(parts) != 2 or parts[0] != "tz":
            if query.message:
                await query.message.reply_text("Некорректные данные выбора. Попробуйте ещё раз: /timezone")
            return
        choice = parts[1]
        if choice == "other":
            context.chat_data["awaiting_timezone_manual"] = True
            context.user_data["awaiting_timezone_manual"] = True
            if query.message:
                await query.message.reply_text(
                    "Введи часовой пояс в формате Europe/Moscow, Europe/Berlin, Asia/Almaty"
                )
            return
        # set selected tz
        try:
            ZoneInfo(choice)
        except Exception:
            if query.message:
                await query.message.reply_text("Неизвестный TZ. Пример: Europe/Moscow, Europe/Berlin, Asia/Almaty")
            return
        if update.effective_user:
            with get_session() as session:
                get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
                set_user_timezone(session, update.effective_user.id, choice)
                session.commit()
        if query.message:
            await query.message.reply_text(f"Часовой пояс обновлён: {choice}")
    except Exception as e:
        logger.exception("handle_tz_choice error: %s", e)
        if query and query.message:
            await query.message.reply_text("Не удалось обновить часовой пояс. Попробуйте ещё раз.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    with get_session() as session:
        user, totals = get_today_totals(session, update.effective_user.id)
    if user is None:
        await update.message.reply_text("Сначала отправьте /start")
        return
    if user.calorie_target is None:
        await update.message.reply_text("Укажите дневной лимит через /target.")
        return
    remaining = max(user.calorie_target - totals["cal_today"], 0)
    await update.message.reply_text(
        f"Текущий лимит: {user.calorie_target} ккал\n"
        f"Съедено сегодня: {totals['cal_today']} ккал\n"
        f"Остаток на сегодня: {remaining} ккал\n"
        f"Часовой пояс: {user.timezone}"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    with get_session() as session:
        user, totals = get_today_totals(session, update.effective_user.id)
        if user is None:
            user = get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
            totals = {"cal_today": 0}
        if user.calorie_target is None:
            await update.message.reply_text("Сначала установите дневной лимит: /target.")
            return
    # Download the highest resolution photo
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        buf = await file.download_as_bytearray()
        image_b64 = base64.b64encode(bytes(buf)).decode("utf-8")
        image_data_url = f"data:image/jpeg;base64,{image_b64}"
    except Exception as e:
        logger.exception("Failed to download photo: %s", e)
        await update.message.reply_text("Не удалось скачать фото. Попробуйте ещё раз.")
        return

    # Call vision provider
    try:
        system_prompt = build_system_prompt()
        analysis = await analyze_meal(image_data_url, system_prompt)
    except Exception as e:
        logger.exception("Vision analyze error: %s", e)
        await update.message.reply_text("Не удалось проанализировать фото. Попробуйте ещё раз.")
        return

    # Extract values
    dish = analysis.get("dish") or "Блюдо"
    portion = analysis.get("portion") or "—"
    calories_est = analysis.get("calories_kcal")
    health_score = analysis.get("health_score")
    recommendation = analysis.get("recommendation") or "—"
    motivation = analysis.get("motivation") or "Отличная работа!"
    low_quality = analysis.get("low_quality", False)

    with get_session() as session:
        user, totals = get_today_totals(session, update.effective_user.id)
        if low_quality:
            reply_text = (
                "Сложно распознать изображение. Пожалуйста, сделайте фото при лучшем освещении и повторите."
            )
            await update.message.reply_text(reply_text)
            return

        calories_number = None
        if isinstance(calories_est, (int, float)):
            calories_number = int(calories_est)

        # Store meal if we have calories
        if calories_number is not None:
            add_meal(
                session=session,
                user_id=user.telegram_id,
                created_at_utc=datetime.now(timezone.utc),
                dish=dish,
                portion=portion,
                calories=calories_number,
                raw_model_json=analysis,
            )
            session.commit()
            # Recompute totals including this meal
            _, totals = get_today_totals(session, update.effective_user.id)

        remaining = None
        if user.calorie_target is not None and totals is not None:
            remaining = max(user.calorie_target - totals["cal_today"], 0)

    reply = format_reply(
        dish=dish,
        portion=portion,
        calories=calories_number,
        health_score=health_score,
        recommendation=recommendation,
        remaining=remaining,
        motivation=motivation,
    )

    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id

    with get_session() as session:
        user = get_or_create_user(session, user_id, DEFAULT_TZ)

    tz = ZoneInfo(user.timezone)
    now_local = datetime.now(tz)

    # parse optional arg: yesterday | YYYY-MM-DD
    day: date
    if context.args:
        arg = context.args[0].strip().lower()
        if arg == "yesterday":
            day = now_local.date() - timedelta(days=1)
        else:
            try:
                year, month, day_ = map(int, arg.split("-"))
                day = date(year, month, day_)
            except Exception:
                await update.message.reply_text("Неверный формат. Используй: /summary, /summary yesterday или /summary YYYY-MM-DD")
                return
    else:
        day = now_local.date()

    with get_session() as session:
        meals = get_meals_for_local_day(session, user_id, day, user.timezone)
        items = [(m.dish, m.portion, m.calories) for m in meals]
        total = get_day_total_calories(session, user_id, day, user.timezone)
        text = format_daily_summary(day.isoformat(), items, total, user.calorie_target)

    await update.message.reply_text(text)


def main() -> None:
    init_db()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(handle_goal_choice, pattern=r"^goal:(lose|maintain|gain)$"))
    application.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^confirm:(yes|edit)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_input))

    # target calories (new) and legacy alias
    application.add_handler(CommandHandler("target", cmd_target))
    application.add_handler(CommandHandler("setcalories", cmd_target))

    # timezone selection
    application.add_handler(CommandHandler("timezone", cmd_timezone))
    application.add_handler(CallbackQueryHandler(handle_tz_choice, pattern=r"^tz:.*$"))

    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    application.add_handler(CommandHandler("summary", cmd_summary))

    # Start the daily summary worker
    asyncio.get_event_loop().create_task(daily_summary_worker(application))

    application.run_polling()


if __name__ == "__main__":
    main() 