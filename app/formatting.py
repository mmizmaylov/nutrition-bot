import random
from datetime import date
from typing import Optional, Union

from app.reminders import EMPTY_DAY_REMINDERS
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
    return "\n".join(lines)


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

    footer: list[str] = []
    if isinstance(target, int):
        delta = target - total_calories
        if abs(delta) <= BULLSEYE_KCAL:
            rest = f"осталось {delta} ккал" if delta >= 0 else f"перебор всего {abs(delta)} ккал"
            footer.append(f"🎯 Почти точно в цель: {rest}")
            footer.append(_pick_daily_phrase(SUMMARY_BULLSEYE, date_str, user_id, "bullseye"))
        elif delta > 0 and day_finished and total_calories < target * UNDER_TARGET_SHARE:
            footer.append(f"🔻 Меньше половины цели: осталось {delta} ккал")
            footer.append(_pick_daily_phrase(SUMMARY_UNDER, date_str, user_id, "under"))
        elif delta > 0:
            footer.append(f"✅ В пределах цели: осталось {delta} ккал")
            footer.append(_pick_daily_phrase(SUMMARY_WITHIN, date_str, user_id, "within"))
        else:
            footer.append(f"⚠️ Перебор на {abs(delta)} ккал")
            footer.append(_pick_daily_phrase(SUMMARY_OVER, date_str, user_id, "over"))
    else:
        footer.append(_pick_daily_phrase(SUMMARY_NO_TARGET, date_str, user_id, "no_target"))

    return "\n".join(header + lines + totals + [""] + footer)


def format_empty_day_reminder(date_str: str, user_id: Optional[int] = None) -> str:
    """Возвращает случайное напоминание о дне без записей."""
    return _pick_daily_phrase(EMPTY_DAY_REMINDERS, date_str, user_id, "empty")


def format_meal_button_label(dish: str, portion: Optional[str], calories: Optional[int]) -> str:
    return (dish or "").strip()


def format_deleted_confirmation() -> str:
    return "🗑️ Блюдо удалено из дневной статистики."


def format_updated_confirmation() -> str:
    return "✅ Запись обновлена."


def format_add_previous_day_button(date_str: str) -> str:
    """Форматирует кнопку для добавления еды за прошедший день"""
    return f"➕ Добавить еду за {date_str}" 