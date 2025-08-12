import os
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _client = AsyncOpenAI(api_key=api_key)
    return _client


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            return "\n".join(lines).strip()
    return t


async def analyze_meal(
    image_data_url: Optional[str] = None, 
    system_prompt: str = "", 
    text_description: Optional[str] = None
) -> Dict[str, Any]:
    """
    Анализирует еду по фото, текстовому описанию или их комбинации.
    
    Args:
        image_data_url: Base64 изображение в формате data:image/jpeg;base64,... (опционально)
        system_prompt: Системный промпт
        text_description: Текстовое описание еды (опционально)
    
    Returns:
        Словарь с анализом еды
    """
    model = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
    client = _get_client()

    # Формируем инструкцию в зависимости от входных данных
    if image_data_url and text_description:
        user_instruction = (
            f"Проанализируй изображение с едой и дополнительное описание: '{text_description}'. "
            "Учти оба источника информации при анализе. "
            "Верни краткую оценку в формате JSON с ключами: "
            "dish (строка), portion (строка), calories_kcal (число), protein_g (число, г), fat_g (число, г), carbs_g (число, г), "
            "health_score (1..5, число или строка), recommendation (строка), motivation (строка), low_quality (boolean). Без комментариев."
        )
        content = [
            {"type": "text", "text": user_instruction},
            {"type": "image_url", "image_url": {"url": image_data_url, "detail": "auto"}},
        ]
    elif image_data_url:
        user_instruction = (
            "Проанализируй изображение с едой и верни краткую оценку. Ответ строго в формате JSON с ключами: "
            "dish (строка), portion (строка), calories_kcal (число), protein_g (число, г), fat_g (число, г), carbs_g (число, г), "
            "health_score (1..5, число или строка), recommendation (строка), motivation (строка), low_quality (boolean). Без комментариев."
        )
        content = [
            {"type": "text", "text": user_instruction},
            {"type": "image_url", "image_url": {"url": image_data_url, "detail": "auto"}},
        ]
    elif text_description:
        user_instruction = (
            f"Проанализируй описание еды: '{text_description}'. "
            "Оцени блюдо, примерный размер порции и калорийность на основе описания. "
            "Верни краткую оценку в формате JSON с ключами: "
            "dish (строка), portion (строка), calories_kcal (число), protein_g (число, г), fat_g (число, г), carbs_g (число, г), "
            "health_score (1..5, число или строка), recommendation (строка), motivation (строка), low_quality (boolean). "
            "Поскольку это текстовое описание, установи low_quality=false. Без комментариев."
        )
        content = [{"type": "text", "text": user_instruction}]
    else:
        # Если ничего не передано, возвращаем заглушку
        return {
            "dish": "Неизвестное блюдо",
            "portion": "—",
            "calories_kcal": None,
            "protein_g": None,
            "fat_g": None,
            "carbs_g": None,
            "health_score": None,
            "recommendation": "Добавьте описание или фото еды",
            "motivation": "Попробуйте еще раз!",
            "low_quality": True,
        }

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        temperature=0.2,
        max_tokens=400,
    )

    content_text = resp.choices[0].message.content or "{}"
    content_text = _strip_code_fences(content_text)
    try:
        import json
        data = json.loads(content_text)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        return {
            "dish": "Блюдо",
            "portion": "—",
            "calories_kcal": None,
            "protein_g": None,
            "fat_g": None,
            "carbs_g": None,
            "health_score": None,
            "recommendation": "—",
            "motivation": "Молодец!",
            "low_quality": False,
        } 