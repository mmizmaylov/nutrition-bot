import asyncio
import base64
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler, filters

from app.db import get_session, init_db, get_or_create_user, set_user_calorie_target, set_user_timezone, get_today_totals
from app.db import add_meal
from app.vision_providers.openai_provider import analyze_meal
from app.prompt import build_system_prompt
from app.formatting import format_reply

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
    context.user_data["proposed_calories"] = proposed
    context.user_data["awaiting_manual_calories"] = False

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
        proposed = context.user_data.get("proposed_calories")
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
        context.user_data.pop("proposed_calories", None)
        context.user_data["awaiting_manual_calories"] = False

    elif action == "edit":
        context.user_data["awaiting_manual_calories"] = True
        if query.message:
            await query.message.reply_text("Введи желаемый дневной лимит в ккал (целое число), например: 1800")


async def handle_manual_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not context.user_data.get("awaiting_manual_calories"):
        return
    text = (update.message.text or "").strip()
    try:
        target = int(text)
        if target <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Пожалуйста, введи положительное целое число, например: 1800")
        return

    with get_session() as session:
        get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
        set_user_calorie_target(session, update.effective_user.id, target)
        session.commit()

    context.user_data["awaiting_manual_calories"] = False
    context.user_data.pop("proposed_calories", None)

    await update.message.reply_text(
        f"✅ Готово! Твой дневной лимит: {target} ккал\n"
        "Теперь просто присылай мне фото еды — я скажу, что в тарелке, оценю калорийность и подскажу, сколько осталось до конца дня 💪"
    )


async def cmd_setcalories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    if not context.args:
        await update.message.reply_text("Использование: /setcalories <ккал>, например: /setcalories 2000")
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


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    if not context.args:
        await update.message.reply_text("Использование: /settz <TZID>, например: /settz Europe/Moscow")
        return
    tzid = context.args[0]
    try:
        ZoneInfo(tzid)
    except Exception:
        await update.message.reply_text("Неизвестный TZID. Пример: Europe/Moscow, Europe/Berlin, Asia/Almaty")
        return
    with get_session() as session:
        get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
        set_user_timezone(session, update.effective_user.id, tzid)
        session.commit()
    await update.message.reply_text(f"Часовой пояс обновлён: {tzid}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    with get_session() as session:
        user, totals = get_today_totals(session, update.effective_user.id)
    if user is None:
        await update.message.reply_text("Сначала отправьте /start")
        return
    if user.calorie_target is None:
        await update.message.reply_text("Укажите дневной лимит через /setcalories <ккал>.")
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
            await update.message.reply_text("Сначала установите дневной лимит: /setcalories <ккал>.")
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


def main() -> None:
    init_db()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(handle_goal_choice, pattern=r"^goal:(lose|maintain|gain)$"))
    application.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^confirm:(yes|edit)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_calories))

    application.add_handler(CommandHandler("setcalories", cmd_setcalories))
    application.add_handler(CommandHandler("settz", cmd_settz))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    application.run_polling()


if __name__ == "__main__":
    main() 