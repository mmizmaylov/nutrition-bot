from typing import Optional


def format_reply(
    dish: str,
    portion: Optional[str],
    calories: Optional[int],
    health_score,
    recommendation: str,
    remaining: Optional[int],
    motivation: str,
) -> str:
    cal_str = f"{calories} ккал" if calories is not None else "—"
    health_str = str(health_score) if health_score is not None else "—"
    remaining_str = f"{remaining} ккал" if remaining is not None else "—"

    # Telegram MarkdownV2 or Markdown: we use plain Markdown here safely
    lines = [
        f"🍽️ Блюдо: {dish}",
        f"📏 Порция: {portion or '—'}",
        f"🔥 Калорийность: {cal_str}",
        f"✅ Оценка пользы: {health_str}",
        f"💡 Рекомендация: {recommendation}",
        f"📉 Остаток на день: {remaining_str}",
        f"💬 Мотивация/похвала: {motivation}",
    ]
    return "\n".join(lines) 