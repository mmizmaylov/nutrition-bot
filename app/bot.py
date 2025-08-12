import asyncio
import base64
import logging
import os
import random
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
import re

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler, filters

from app.db import get_session, init_db, get_or_create_user, set_user_calorie_target, set_user_timezone, get_today_totals
from app.db import add_meal
from app.db import get_all_users, get_meals_for_local_day, get_day_total_calories, has_summary_sent, mark_summary_sent
from app.db import get_meal_by_id, delete_meal_by_id
from app.vision_providers.openai_provider import analyze_meal
from app.prompt import build_system_prompt
from app.formatting import (
    format_reply,
    format_daily_summary,
    format_empty_day_reminder,
    format_meal_button_label,
    format_deleted_confirmation,
    format_updated_confirmation,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")

# Варианты сообщений о загрузке
LOADING_MESSAGES = [
    "🤔 Анализирую...",
    "🔍 Изучаю блюдо...",
    "⚡ Обрабатываю данные...",
    "🧠 Думаю...",
    "📊 Считаю калории...",
    "🔄 Анализирую питательность...",
    "⏳ Секундочку...",
    "🎯 Определяю состав...",
]

async def send_loading_message(update: Update) -> Message | None:
    """Отправляет случайное сообщение о загрузке"""
    if not update.message:
        return None
    
    try:
        loading_text = random.choice(LOADING_MESSAGES)
        loading_message = await update.message.reply_text(loading_text)
        return loading_message
    except Exception as e:
        logger.exception("Failed to send loading message: %s", e)
        return None

async def delete_loading_message(loading_message: Message | None) -> None:
    """Удаляет временное сообщение о загрузке"""
    if loading_message:
        try:
            await loading_message.delete()
        except Exception as e:
            logger.exception("Failed to delete loading message: %s", e)

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
                    items = [(m.dish, m.portion, m.calories, m.protein_g, m.fat_g, m.carbs_g) for m in meals]
                    total = get_day_total_calories(session, user.telegram_id, day_yesterday, user.timezone)

                    # If no meals, send polite reminder instead of summary
                    if not items:
                        text = format_empty_day_reminder(day_str)
                    else:
                        total_protein = sum(int(m.protein_g) for m in meals if isinstance(m.protein_g, int))
                        total_fat = sum(int(m.fat_g) for m in meals if isinstance(m.fat_g, int))
                        total_carbs = sum(int(m.carbs_g) for m in meals if isinstance(m.carbs_g, int))
                        text = format_daily_summary(date_str=day_str, items=items, total_calories=total, totals_macros=(total_protein, total_fat, total_carbs), target=user.calorie_target)

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
                "Теперь присылай мне:\n"
                "📸 Фото еды — я распознаю блюдо и оценю калорийность\n"
                "✍️ Текстовое описание — например, \"паста карбонара\" или \"200г риса\"\n"
                "📸+✍️ Фото с подписью — для более точного анализа\n\n"
                "Я подскажу, сколько калорий осталось до конца дня 💪"
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
            "Теперь присылай мне:\n"
            "📸 Фото еды — я распознаю блюдо и оценю калорийность\n"
            "✍️ Текстовое описание — например, \"паста карбонара\" или \"200г риса\"\n"
            "📸+✍️ Фото с подписью — для более точного анализа\n\n"
            "Я подскажу, сколько калорий осталось до конца дня 💪"
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

    # If none of the above matched, try to analyze as food description
    # Only process as food if user has calorie target set
    with get_session() as session:
        user, totals = get_today_totals(session, update.effective_user.id)
        if user is not None and user.calorie_target is not None:
            # This looks like a food description, process it
            await _analyze_text_as_food(update, text)
            return
    
    # If no calorie target set, ignore silently
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
    
    # Отправляем сообщение о загрузке
    loading_message = await send_loading_message(update)
    
    try:
        with get_session() as session:
            user, totals = get_today_totals(session, update.effective_user.id)
            if user is None:
                user = get_or_create_user(session, update.effective_user.id, DEFAULT_TZ)
                totals = {"cal_today": 0}
            if user.calorie_target is None:
                await delete_loading_message(loading_message)
                await update.message.reply_text("Сначала установите дневной лимит: /target.")
                return
        
        # Получаем текст подписи к фото (если есть)
        photo_caption = (update.message.caption or "").strip()
        
        # Download the highest resolution photo
        try:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            buf = await file.download_as_bytearray()
            image_b64 = base64.b64encode(bytes(buf)).decode("utf-8")
            image_data_url = f"data:image/jpeg;base64,{image_b64}"
        except Exception as e:
            logger.exception("Failed to download photo: %s", e)
            await delete_loading_message(loading_message)
            await update.message.reply_text("Не удалось скачать фото. Попробуйте ещё раз.")
            return

        # Call vision provider with photo and optional text description
        try:
            system_prompt = build_system_prompt()
            analysis = await analyze_meal(
                image_data_url=image_data_url, 
                system_prompt=system_prompt,
                text_description=photo_caption if photo_caption else None
            )
        except Exception as e:
            logger.exception("Vision analyze error: %s", e)
            await delete_loading_message(loading_message)
            await update.message.reply_text("Не удалось проанализировать фото. Попробуйте ещё раз.")
            return

        # Удаляем сообщение о загрузке перед отправкой результата
        await delete_loading_message(loading_message)
        
        # Extract values and process the result (same as before)
        await _process_food_analysis(update, analysis)
        
    except Exception as e:
        # В случае любой ошибки удаляем сообщение о загрузке
        await delete_loading_message(loading_message)
        logger.exception("Unexpected error in handle_photo: %s", e)
        await update.message.reply_text("Произошла ошибка при обработке фото. Попробуйте ещё раз.")


async def _analyze_text_as_food(update: Update, text_description: str) -> None:
    """Анализирует текстовое описание как еду"""
    assert update.message is not None
    assert update.effective_user is not None
    
    # Отправляем сообщение о загрузке
    loading_message = await send_loading_message(update)
    
    try:
        # Call vision provider with text description only
        system_prompt = build_system_prompt()
        analysis = await analyze_meal(
            system_prompt=system_prompt,
            text_description=text_description
        )
        
        # Удаляем сообщение о загрузке перед отправкой результата
        await delete_loading_message(loading_message)
        
        # Process the result
        await _process_food_analysis(update, analysis)
        
    except Exception as e:
        # В случае ошибки удаляем сообщение о загрузке
        await delete_loading_message(loading_message)
        logger.exception("Text analyze error: %s", e)
        await update.message.reply_text("Не удалось проанализировать описание. Попробуйте ещё раз.")


async def _process_food_analysis(update: Update, analysis: dict) -> None:
    """Общая логика обработки результата анализа еды"""
    assert update.message is not None
    assert update.effective_user is not None
    
    # Extract values
    dish = analysis.get("dish") or "Блюдо"
    portion = analysis.get("portion") or "—"
    calories_est = analysis.get("calories_kcal")
    protein_est = analysis.get("protein_g")
    fat_est = analysis.get("fat_g")
    carbs_est = analysis.get("carbs_g")
    health_score = analysis.get("health_score")
    recommendation = analysis.get("recommendation") or "—"
    motivation = analysis.get("motivation") or "Отличная работа!"
    low_quality = analysis.get("low_quality", False)

    with get_session() as session:
        user, totals = get_today_totals(session, update.effective_user.id)
        if low_quality:
            reply_text = (
                "Сложно распознать или проанализировать. Пожалуйста, добавьте более подробное описание или сделайте фото при лучшем освещении."
            )
            await update.message.reply_text(reply_text)
            return

        calories_number = None
        protein_number = None
        fat_number = None
        carbs_number = None
        if isinstance(calories_est, (int, float)):
            calories_number = int(calories_est)
        if isinstance(protein_est, (int, float)):
            protein_number = int(protein_est)
        if isinstance(fat_est, (int, float)):
            fat_number = int(fat_est)
        if isinstance(carbs_est, (int, float)):
            carbs_number = int(carbs_est)

        # Store meal if we have calories
        if calories_number is not None:
            add_meal(
                session=session,
                user_id=user.telegram_id,
                created_at_utc=datetime.now(timezone.utc),
                dish=dish,
                portion=portion,
                calories=calories_number,
                protein_g=protein_number,
                fat_g=fat_number,
                carbs_g=carbs_number,
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
        protein_g=protein_number,
        fat_g=fat_number,
        carbs_g=carbs_number,
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
        items = [(m.dish, m.portion, m.calories, m.protein_g, m.fat_g, m.carbs_g) for m in meals]
        total = get_day_total_calories(session, user_id, day, user.timezone)
        if not items:
            # If querying today, just say there are no records yet; no reminder
            if day == now_local.date():
                text = "За сегодня пока нет записей."
            else:
                # Show regular summary with 'не зафиксировано'
                text = format_daily_summary(day.isoformat(), items, total, (0, 0, 0), user.calorie_target)
        else:
            # Aggregate macros
            total_protein = sum(int(m.protein_g) for m in meals if isinstance(m.protein_g, int))
            total_fat = sum(int(m.fat_g) for m in meals if isinstance(m.fat_g, int))
            total_carbs = sum(int(m.carbs_g) for m in meals if isinstance(m.carbs_g, int))
            text = format_daily_summary(day.isoformat(), items, total, (total_protein, total_fat, total_carbs), user.calorie_target)

    await update.message.reply_text(text)


def _build_today_meals_keyboard(user_id: int, tzid: str) -> InlineKeyboardMarkup | None:
    from app.db import get_meals_for_local_day
    from datetime import datetime
    tz = ZoneInfo(tzid)
    day = datetime.now(tz).date()
    with get_session() as session:
        meals = get_meals_for_local_day(session, user_id, day, tzid)
    if not meals:
        return None
    buttons = []
    for m in meals:
        label = format_meal_button_label(m.dish, m.portion, m.calories)
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"meal:{m.id}")])
    # Add cancel button at the bottom
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="abort")])
    return InlineKeyboardMarkup(buttons)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    with get_session() as session:
        user, _ = get_today_totals(session, update.effective_user.id)
    if user is None:
        await update.message.reply_text("Сначала отправьте /start")
        return
    kb = _build_today_meals_keyboard(user.telegram_id, user.timezone)
    if kb is None:
        await update.message.reply_text("За сегодня пока нет записей.")
        return
    await update.message.reply_text("Выберите блюдо для удаления:", reply_markup=kb)


async def handle_cancel_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("meal:"):
        return
    meal_id_str = data.split(":", 1)[1]
    try:
        meal_id = int(meal_id_str)
    except Exception:
        return
    user = update.effective_user
    if not user:
        return
    with get_session() as session:
        ok = delete_meal_by_id(session, meal_id, user.id)
        if ok:
            session.commit()
    if query.message:
        await query.message.reply_text(format_deleted_confirmation())


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    assert update.effective_user is not None
    with get_session() as session:
        user, _ = get_today_totals(session, update.effective_user.id)
    if user is None:
        await update.message.reply_text("Сначала отправьте /start")
        return
    kb = _build_today_meals_keyboard(user.telegram_id, user.timezone)
    if kb is None:
        await update.message.reply_text("За сегодня пока нет записей.")
        return
    await update.message.reply_text("Выберите блюдо для корректировки:", reply_markup=kb)


async def handle_edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("meal:"):
        return
    meal_id_str = data.split(":", 1)[1]
    try:
        meal_id = int(meal_id_str)
    except Exception:
        return
    user = update.effective_user
    if not user:
        return
    # Remember which meal we edit and expect next message as text or photo
    context.user_data["editing_meal_id"] = meal_id
    context.user_data["awaiting_edit_input"] = True
    if query.message:
        await query.message.reply_text("Отправьте уточнение текстом или новое фото для переоценки блюда.")


async def handle_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    # Clear any edit-related state
    context.user_data["editing_meal_id"] = None
    context.user_data["awaiting_edit_input"] = False
    if query.message:
        await query.message.reply_text("Действие отменено.")


async def _apply_edit_to_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    meal_id = context.user_data.get("editing_meal_id")
    if not isinstance(meal_id, int):
        return
    # Prepare new analysis based on user input
    new_analysis: dict | None = None
    system_prompt = build_system_prompt()
    # Text clarification
    if update.message and update.message.text:
        desc = (update.message.text or "").strip()
        if not desc:
            return
        new_analysis = await analyze_meal(system_prompt=system_prompt, text_description=desc)
    # New photo
    elif update.message and update.message.photo:
        try:
            photo = update.message.photo[-1]
            file = await photo.get_file()
            buf = await file.download_as_bytearray()
            b64 = base64.b64encode(bytes(buf)).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{b64}"
            new_analysis = await analyze_meal(image_data_url=data_url, system_prompt=system_prompt)
        except Exception:
            logger.exception("Failed to download photo for edit")
            if update.message:
                await update.message.reply_text("Не удалось скачать фото. Попробуйте ещё раз.")
            return
    else:
        return

    if not isinstance(new_analysis, dict):
        return

    # Extract values
    dish = new_analysis.get("dish") or None
    portion = new_analysis.get("portion") or None
    cal = new_analysis.get("calories_kcal")
    protein_est = new_analysis.get("protein_g")
    fat_est = new_analysis.get("fat_g")
    carbs_est = new_analysis.get("carbs_g")

    cal_num = int(cal) if isinstance(cal, (int, float)) else None
    p_num = int(protein_est) if isinstance(protein_est, (int, float)) else None
    f_num = int(fat_est) if isinstance(fat_est, (int, float)) else None
    c_num = int(carbs_est) if isinstance(carbs_est, (int, float)) else None

    with get_session() as session:
        meal = get_meal_by_id(session, meal_id, update.effective_user.id)
        if meal is None:
            if update.message:
                await update.message.reply_text("Запись не найдена.")
            return
        if dish:
            meal.dish = dish
        if portion is not None:
            meal.portion = portion
        if cal_num is not None:
            meal.calories = cal_num
        if p_num is not None:
            meal.protein_g = p_num
        if f_num is not None:
            meal.fat_g = f_num
        if c_num is not None:
            meal.carbs_g = c_num
        # Save raw
        try:
            import json as _json
            meal.raw_model_json = _json.dumps(new_analysis, ensure_ascii=False)
        except Exception:
            pass
        session.commit()
    # Clear edit state
    context.user_data["editing_meal_id"] = None
    context.user_data["awaiting_edit_input"] = False

    if update.message:
        await update.message.reply_text(format_updated_confirmation())


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Route next text/photo after /edit meal selection to apply edit
    if context.user_data.get("awaiting_edit_input"):
        await _apply_edit_to_meal(update, context)
        return
    # Otherwise, fall back to existing manual input or photo handling
    if update.message and update.message.text:
        await handle_manual_input(update, context)
    elif update.message and update.message.photo:
        await handle_photo(update, context)


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

    # cancel/edit flows
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("edit", cmd_edit))
    application.add_handler(CallbackQueryHandler(handle_cancel_choice, pattern=r"^meal:\d+$"))
    application.add_handler(CallbackQueryHandler(handle_edit_choice, pattern=r"^meal:\d+$"))
    application.add_handler(CallbackQueryHandler(handle_abort, pattern=r"^abort$"))
    # After selecting a meal to edit, the next text/photo is routed here
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_edit_input))

    # Start the daily summary worker
    asyncio.get_event_loop().create_task(daily_summary_worker(application))

    application.run_polling()


if __name__ == "__main__":
    main() 