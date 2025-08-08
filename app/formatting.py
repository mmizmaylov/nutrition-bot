from typing import Optional, Union


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
    health_score: Optional[Union[int, float, str]],
    recommendation: str,
    remaining: Optional[int],
    motivation: str,
) -> str:
    cal_str = f"{calories} ккал" if calories is not None else "—"
    remaining_str = f"{remaining} ккал" if remaining is not None else "—"
    stars = _health_to_stars(health_score)

    lines = [
        f"{dish}",
        "",
        f"🍽️ Порция: {portion or '—'}",
        f"🔥 Калорийность: {cal_str}",
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
    items: list[tuple[str, Optional[str], Optional[int]]],
    total_calories: int,
    target: Optional[int],
) -> str:
    header = [
        f"📅 Итоги дня — {date_str}",
        "",
    ]

    if items:
        lines = ["🍽️ Съедено:"]
        for dish, portion, cal in items:
            portion_txt = f" · {portion}" if portion else ""
            cal_txt = f" — {cal} ккал" if cal is not None else ""
            lines.append(f"• {dish}{portion_txt}{cal_txt}")
    else:
        lines = ["🍽️ За день приёмов пищи не зафиксировано"]

    totals = [
        "",
        f"🔥 Итого за день: {total_calories} ккал",
    ]

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