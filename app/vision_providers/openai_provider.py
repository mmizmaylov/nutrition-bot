import os
from typing import Any, Dict

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


async def analyze_meal(image_data_url: str, system_prompt: str) -> Dict[str, Any]:
    model = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
    client = _get_client()

    user_instruction = (
        "Проанализируй изображение с едой и верни краткую оценку. Ответ строго в формате JSON с ключами: "
        "dish (строка), portion (строка), calories_kcal (число), health_score (1..5, число или строка), "
        "recommendation (строка), motivation (строка), low_quality (boolean). Без комментариев."
    )

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_instruction},
                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": "auto"}},
                ],
            },
        ],
        temperature=0.2,
        max_tokens=400,
    )

    content = resp.choices[0].message.content or "{}"
    content = _strip_code_fences(content)
    try:
        import json
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        return {
            "dish": "Блюдо",
            "portion": "—",
            "calories_kcal": None,
            "health_score": None,
            "recommendation": "—",
            "motivation": "Молодец!",
            "low_quality": False,
        } 