### Nutrition Telegram Bot (ru)

Бот помогает отслеживать питание по фото: распознаёт блюдо, оценивает порцию и калорийность, хранит дневную норму и подсчитывает остаток калорий.

#### Возможности
- Установка целевой калорийности в день
- Приём фото, анализ через OpenAI Vision (gpt-4o-mini по умолчанию)
- Хранение дневного потребления в SQLite
- Команды: `/start`, `/target`, `/timezone [TZID]`, `/status`

#### Альтернативы API распознавания
- OpenAI GPT-4o/4o-mini (в коде по умолчанию)
- Google Gemini 1.5 Flash/Pro Vision
- Anthropic Claude 3.5 Sonnet Vision
- DeepSeek-VL (через их API)

Структура кода предусматривает выделение провайдера, можно добавить новый провайдер по аналогии с `app/vision_providers/openai_provider.py`.

#### Установка
1) Python 3.11+
2) Установить зависимости:

```bash
pip install -r requirements.txt
```

3) Создайте файл `.env` в корне и задайте переменные:

```bash
TELEGRAM_BOT_TOKEN=...  # токен бота
OPENAI_API_KEY=...      # ключ OpenAI
OPENAI_VISION_MODEL=gpt-4o-mini  # опционально
DEFAULT_TIMEZONE=Europe/Moscow   # опционально
```

4) Запуск бота:

```bash
python -m app.bot
```

#### Использование
- Отправьте `/start` в чате с ботом. Для установки лимита используйте `/target` — бот попросит ввести число (можно также `/target 2000`). Для часового пояса используйте `/timezone` или `/timezone Europe/Paris`.
- Отправляйте фото еды — бот ответит оформленным сообщением и посчитает остаток калорий на сегодня

#### Хранилище
- SQLite база `nutrition.db` создаётся автоматически в корне проекта

#### Примечания по приватности
- Изображение отправляется во внешний API распознавания (OpenAI). Для локальной приватной альтернативы используйте собственный провайдер/хостинг модели. 

#### Обновление на сервере (Docker Compose)

```bash
cd /opt/nutrition-bot
sudo git pull
sudo docker compose up -d --build
``` 

#### Диагностика (на сервере)

- Логи бота в реальном времени:

```bash
sudo docker compose -f /opt/nutrition-bot/docker-compose.yml logs -f
# или
sudo docker logs -f nutrition-bot
```

- Проверка состояния контейнера:

```bash
sudo docker ps --filter name=nutrition-bot
```

- Просмотр данных SQLite:

```bash
# скопировать базу из контейнера
sudo docker cp nutrition-bot:/app/nutrition.db /tmp/nutrition.db

# установить cli (один раз)
sudo apt-get update && sudo apt-get install -y sqlite3

# список таблиц
sqlite3 /tmp/nutrition.db '.tables'

# пользователи
sqlite3 /tmp/nutrition.db 'SELECT telegram_id, calorie_target, timezone FROM users ORDER BY telegram_id;'

# сколько фото у каждого пользователя
sqlite3 /tmp/nutrition.db 'SELECT u.telegram_id, COALESCE(COUNT(m.id),0) AS photos FROM users u LEFT JOIN meals m ON m.user_id=u.telegram_id GROUP BY u.telegram_id ORDER BY photos DESC;'

# последние 20 приёмов пищи
sqlite3 /tmp/nutrition.db 'SELECT id, user_id, dish, calories, created_at_utc FROM meals ORDER BY id DESC LIMIT 20;'
``` 