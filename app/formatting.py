import random
from datetime import date
from typing import Optional, Union

from app.meal_phrases import (
    MEAL_BULLSEYE,
    MEAL_CLOSE,
    MEAL_JUST_OVER,
    MEAL_PLENTY,
    MEAL_STILL_OVER,
)
from app.reminders import EMPTY_DAY_REMINDERS
from app.service_phrases import (
    CANCELLED_CONFIRMATIONS,
    DELETED_CONFIRMATIONS,
    LOADING_MESSAGES,
    LOW_QUALITY_MESSAGES,
)
from app.summary_phrases import (
    SUMMARY_BULLSEYE,
    SUMMARY_NO_TARGET,
    SUMMARY_OVER,
    SUMMARY_UNDER,
    SUMMARY_WITHIN,
)

# Отклонение от цели (ккал), при котором день считается попаданием «почти точно в цель»
BULLSEYE_KCAL = 50
# Доля цели, ниже которой завершенный день считается недобором
UNDER_TARGET_SHARE = 0.5
# Доля цели, выше которой остаток после приема пищи считается большим запасом
MEAL_PLENTY_SHARE = 0.4

# Последняя показанная фраза для пары (пользователь, пул), чтобы не повторять ее подряд.
# Хранится в памяти процесса и сбрасывается при перезапуске бота.
_last_phrases: dict[tuple[Optional[int], str], str] = {}


def _pick_fresh_phrase(pool: tuple[str, ...], user_id: Optional[int], key: str) -> str:
    """Выбирает случайную фразу из пула, не повторяя предыдущую для этого пользователя."""
    last = _last_phrases.get((user_id, key))
    phrase = random.choice([p for p in pool if p != last] or pool)
    _last_phrases[(user_id, key)] = phrase
    return phrase


def _pick_daily_phrase(pool: tuple[str, ...], date_str: str, user_id: Optional[int], key: str) -> str:
    """Выбирает фразу из пула на указанный день.

    Для каждого пользователя фразы перемешиваются в свой порядок и идут по дням,
    поэтому внутри цикла из len(pool) дней ни одна не повторяется.
    """
    if user_id is None:
        return random.choice(pool)
    cycle, position = divmod(date.fromisoformat(date_str).toordinal(), len(pool))
    order = list(range(len(pool)))
    random.Random(f"{user_id}:{key}:{cycle}").shuffle(order)
    return pool[order[position]]


def _health_to_stars(health_score: Optional[Union[int, float, str]]) -> str:
    if health_score is None:
        return "—"
    try:
        value = int(round(float(health_score)))
        value = max(1, min(value, 5))
        return "⭐️" * value
    except Exception:
        return str(health_score)


def format_reply(
    dish: str,
    portion: Optional[str],
    calories: Optional[int],
    protein_g: Optional[int],
    fat_g: Optional[int],
    carbs_g: Optional[int],
    health_score: Optional[Union[int, float, str]],
    recommendation: str,
    remaining: Optional[int],
    motivation: str,
    remaining_phrase: Optional[str] = None,
) -> str:
    cal_str = f"{calories} ккал" if calories is not None else "—"
    remaining_str = f"{remaining} ккал" if remaining is not None else "—"
    stars = _health_to_stars(health_score)

    macros_line = None
    macros_parts: list[str] = []
    if protein_g is not None:
        macros_parts.append(f"Белки: {protein_g} г")
    if fat_g is not None:
        macros_parts.append(f"Жиры: {fat_g} г")
    if carbs_g is not None:
        macros_parts.append(f"Углеводы: {carbs_g} г")
    if macros_parts:
        macros_line = " | ".join(macros_parts)

    lines = [
        f"{dish}",
        "",
        f"🍽️ Порция: {portion or '—'}",
        f"🔥 Калорийность: {cal_str}",
    ]
    if macros_line:
        lines.append(f"📊 {macros_line}")
    lines += [
        "",
        f"Оценка пользы: {stars}",
        "",
        f"💡{recommendation}",
        "",
        f"💬 {motivation}",
        "",
        f"⚖️ Остаток на день: {remaining_str}",
    ]
    if remaining_phrase:
        lines.append(remaining_phrase)
    return "\n".join(lines)


def format_meal_phrase(
    target: Optional[int],
    total_calories: int,
    meal_calories: Optional[int],
    user_id: Optional[int] = None,
) -> Optional[str]:
    """Возвращает фразу под остатком на день после приема пищи.

    total_calories уже включает это блюдо, meal_calories нужен, чтобы понять,
    перешел ли пользователь лимит именно им.
    """
    if target is None:
        return None
    delta = target - total_calories
    if abs(delta) <= BULLSEYE_KCAL:
        pool, key = MEAL_BULLSEYE, "meal_bullseye"
    elif delta < 0:
        if delta + (meal_calories or 0) >= 0:
            pool, key = MEAL_JUST_OVER, "meal_just_over"
        else:
            pool, key = MEAL_STILL_OVER, "meal_still_over"
    elif delta > target * MEAL_PLENTY_SHARE:
        pool, key = MEAL_PLENTY, "meal_plenty"
    else:
        pool, key = MEAL_CLOSE, "meal_close"
    return _pick_fresh_phrase(pool, user_id, key)


def format_loading_message(user_id: Optional[int] = None) -> str:
    return _pick_fresh_phrase(LOADING_MESSAGES, user_id, "loading")


def format_low_quality_message(user_id: Optional[int] = None) -> str:
    return _pick_fresh_phrase(LOW_QUALITY_MESSAGES, user_id, "low_quality")


def format_daily_summary(
    date_str: str,
    items: list[tuple[str, Optional[str], Optional[int], Optional[int], Optional[int], Optional[int]]],
    total_calories: int,
    totals_macros: Optional[tuple[int, int, int]],
    target: Optional[int],
    user_id: Optional[int] = None,
    day_finished: bool = True,
) -> str:
    header = [
        f"📅 Итоги дня — {date_str}",
        "",
    ]

    if items:
        lines = ["🍽️ Съедено:"]
        for dish, portion, cal, p, f, c in items:
            portion_txt = f" · {portion}" if portion else ""
            cal_txt = f" — {cal} ккал" if cal is not None else ""
            macros_parts: list[str] = []
            if p is not None:
                macros_parts.append(f"Б:{p}г")
            if f is not None:
                macros_parts.append(f"Ж:{f}г")
            if c is not None:
                macros_parts.append(f"У:{c}г")
            macros_txt = f" ({', '.join(macros_parts)})" if macros_parts else ""
            lines.append(f"• {dish}{portion_txt}{cal_txt}{macros_txt}")
    else:
        lines = ["🍽️ За день приёмов пищи не зафиксировано"]

    totals = [
        "",
        f"🔥 Итого за день: {total_calories} ккал",
    ]
    if totals_macros is not None:
        tp, tf, tc = totals_macros
        totals.append(f"📊 КБЖУ за день: Б:{tp}г · Ж:{tf}г · У:{tc}г")

    # Шуточная фраза добавляется только к итогам завершенного дня
    footer: list[str] = []
    if isinstance(target, int):
        delta = target - total_calories
        if abs(delta) <= BULLSEYE_KCAL:
            rest = f"осталось {delta} ккал" if delta >= 0 else f"перебор всего {abs(delta)} ккал"
            footer.append(f"🎯 Почти точно в цель: {rest}")
            pool, key = SUMMARY_BULLSEYE, "bullseye"
        elif delta > 0 and day_finished and total_calories < target * UNDER_TARGET_SHARE:
            footer.append(f"🔻 Меньше половины цели: осталось {delta} ккал")
            pool, key = SUMMARY_UNDER, "under"
        elif delta > 0:
            footer.append(f"✅ В пределах цели: осталось {delta} ккал")
            pool, key = SUMMARY_WITHIN, "within"
        else:
            footer.append(f"⚠️ Перебор на {abs(delta)} ккал")
            pool, key = SUMMARY_OVER, "over"
    else:
        if not day_finished:
            footer.append("ℹ️ Цель на день не установлена. Укажи через /target")
        pool, key = SUMMARY_NO_TARGET, "no_target"
    if day_finished:
        footer.append(_pick_daily_phrase(pool, date_str, user_id, key))

    return "\n".join(header + lines + totals + [""] + footer)


def format_empty_day_reminder(date_str: str, user_id: Optional[int] = None) -> str:
    """Возвращает случайное напоминание о дне без записей."""
    return _pick_daily_phrase(EMPTY_DAY_REMINDERS, date_str, user_id, "empty")


def format_meal_button_label(dish: str, portion: Optional[str], calories: Optional[int]) -> str:
    return (dish or "").strip()


def format_deleted_confirmation(user_id: Optional[int] = None) -> str:
    return _pick_fresh_phrase(DELETED_CONFIRMATIONS, user_id, "deleted")


def format_cancelled_confirmation(user_id: Optional[int] = None) -> str:
    return _pick_fresh_phrase(CANCELLED_CONFIRMATIONS, user_id, "cancelled")


def format_updated_confirmation() -> str:
    return "✅ Запись обновлена."


def format_add_previous_day_button(date_str: str) -> str:
    """Форматирует кнопку для добавления еды за прошедший день"""
    return f"➕ Добавить еду за {date_str}" 