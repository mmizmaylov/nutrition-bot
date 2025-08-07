import asyncio
import base64
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from app.db import get_session, init_db, get_or_create_user, set_user_calorie_target, set_user_timezone, get_today_totals
from app.db import add_meal
from app.vision_providers.openai_provider import analyze_meal
from app.prompt import build_system_prompt
from app.formatting import format_reply

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with get_session() as session:
        user = get_or_create_user(session, user_id, DEFAULT_TZ)
        session.commit()
    await update.message.reply_text(
        "Привет! Я помогу отслеживать питание по фото. Укажи дневной лимит калорий командой:\n"
        "/setcalories 2000\n\n"
        "Часовой пояс можно сменить: /settz Europe/Moscow\n"
        "Покажи статус: /status\n\n"
        "Теперь просто пришли мне фото еды."
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
    application.add_handler(CommandHandler("setcalories", cmd_setcalories))
    application.add_handler(CommandHandler("settz", cmd_settz))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    application.run_polling()


if __name__ == "__main__":
    main() 