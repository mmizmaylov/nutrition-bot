import random
from datetime import date
from typing import Optional, Union

from app.reminders import EMPTY_DAY_REMINDERS


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
        if delta >= 0:
            footer.append(f"✅ В пределах цели: осталось {delta} ккал")
            footer.append("👏 Отличная дисциплина! Продолжай в том же духе — стабильность важнее идеальности.")
        else:
            footer.append(f"⚠️ Перебор на {abs(delta)} ккал")
            footer.append("💪 Ничего страшного! Компенсируй сегодня дополнительной активностью, а завтра добавь больше овощей и лёгких блюд.")
    else:
        footer.append("ℹ️ Цель на день не установлена. Укажи через /target")

    return "\n".join(header + lines + totals + [""] + footer)


def format_empty_day_reminder(date_str: str, user_id: Optional[int] = None) -> str:
    """Возвращает случайное напоминание о пустом дне.

    Для каждого пользователя тексты перемешиваются в свой порядок и идут по дням,
    поэтому внутри цикла из len(EMPTY_DAY_REMINDERS) дней ни один не повторяется.
    """
    if user_id is None:
        return random.choice(EMPTY_DAY_REMINDERS)
    cycle, position = divmod(date.fromisoformat(date_str).toordinal(), len(EMPTY_DAY_REMINDERS))
    order = list(range(len(EMPTY_DAY_REMINDERS)))
    random.Random(f"{user_id}:{cycle}").shuffle(order)
    return EMPTY_DAY_REMINDERS[order[position]]


def format_meal_button_label(dish: str, portion: Optional[str], calories: Optional[int]) -> str:
    return (dish or "").strip()


def format_deleted_confirmation() -> str:
    return "🗑️ Блюдо удалено из дневной статистики."


def format_updated_confirmation() -> str:
    return "✅ Запись обновлена."


def format_add_previous_day_button(date_str: str) -> str:
    """Форматирует кнопку для добавления еды за прошедший день"""
    return f"➕ Добавить еду за {date_str}" 