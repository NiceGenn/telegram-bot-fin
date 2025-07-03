# =================================================================================
#    ФИНАЛЬНАЯ ВЕРСИЯ БОТА (V33 - РЕЖИМЫ MAIN/WORKER ДЛЯ СКАЧИВАНИЯ)
# =================================================================================

# --- 1. ИМПОРТЫ ---
import os
import logging
from datetime import datetime
import zipfile
import io
from typing import List, Dict, Any, Optional, Tuple, Set
import asyncio
import psycopg2
import uuid
import re

import uvicorn
from fastapi import FastAPI
import yt_dlp

from telegram import Update, ReplyKeyboardMarkup, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from openpyxl import Workbook
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from dotenv import load_dotenv

load_dotenv()


# --- 2. НАСТРОЙКА И КОНСТАНТЫ ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_MODE = os.environ.get("BOT_MODE", "main")  # По умолчанию 'main'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_USER_IDS: Set[int] = {96238783}
user_filter = filters.User(user_id=ALLOWED_USER_IDS)

# ... (остальные константы без изменений)


# --- 3. ВЕБ-СЕРВЕР FASTAPI (Только для режима 'main') ---
app = FastAPI(docs_url=None, redoc_url=None)

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "bot is running"}


# --- 4. РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db_connection():
    # ... (код без изменений)
    pass

def init_database():
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            # Таблица для настроек
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    threshold INTEGER NOT NULL
                )
            ''')
            # <<< НОВОЕ: Таблица для заданий на скачивание >>>
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS download_tasks (
                    task_id UUID PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    youtube_url TEXT NOT NULL,
                    status VARCHAR(20) DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        conn.commit()
        logger.info("База данных успешно инициализирована.")
    except psycopg2.Error as e:
        logger.error(f"Ошибка при инициализации таблиц: {e}", exc_info=True)
    finally:
        if conn: conn.close()

# ... (функции save_user_threshold, load_user_threshold, get_user_threshold без изменений)

def create_download_task(user_id: int, youtube_url: str) -> Optional[str]:
    """Создает новое задание на скачивание в БД и возвращает ID задания."""
    conn = get_db_connection()
    if not conn: return None
    task_id = uuid.uuid4()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO download_tasks (task_id, user_id, youtube_url) VALUES (%s, %s, %s)",
                (task_id, user_id, youtube_url)
            )
        conn.commit()
        logger.info(f"Создано задание {task_id} для пользователя {user_id}")
        return str(task_id)
    except psycopg2.Error as e:
        logger.error(f"Ошибка при создании задания: {e}", exc_info=True)
        return None
    finally:
        if conn: conn.close()
        
# ... (остальные функции БД без изменений)

# --- 5. ОБРАБОТЧИКИ И ЛОГИКА ---

# <<< НОВОЕ: Обработчик для ссылок YouTube >>>
YOUTUBE_URL_PATTERN = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'

async def handle_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает ссылку на YouTube, создавая задание в БД."""
    url = update.message.text
    task_id = create_download_task(update.effective_user.id, url)

    if task_id:
        keyboard = [[InlineKeyboardButton("▶️ Начать скачивание", callback_data=f"download_{task_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "✅ Задание на скачивание создано.\n\n"
            "Запустите 'рабочего' бота на вашем ПК и нажмите кнопку ниже, чтобы начать.",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text("❌ Не удалось создать задание на скачивание. Попробуйте позже.")


# <<< НОВОЕ: Логика для "Рабочего" бота >>>
async def process_download_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатие кнопки 'Начать скачивание'."""
    query = update.callback_query
    await query.answer()
    task_id = query.data.split('_')[1]

    # ... (здесь будет логика скачивания, аналогичная той, что я присылал ранее)
    # 1. Получить детали задания из БД по task_id
    # 2. Скачать видео с помощью yt-dlp
    # 3. Отправить видео пользователю
    # 4. Удалить файл с диска
    # 5. Обновить статус задания в БД
    await query.edit_message_text(f"Начинаю обработку задания {task_id[:8]}...\n(Эта функция в разработке, но кнопка работает!)")

# ... (все остальные обработчики без изменений)


# --- 6. ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
async def main() -> None:
    init_database()
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        logger.error("Не найден токен или URL базы данных."); return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # --- Регистрация обработчиков ---
    if BOT_MODE == "worker":
        # "Рабочий" бот слушает только команды на скачивание
        application.add_handler(CallbackQueryHandler(process_download_task, pattern='^download_'))
        logger.info("Бот запущен в режиме 'Рабочий'. Ожидает заданий на скачивание...")
    else: # Режим "main"
        # "Главный" бот регистрирует все остальные обработчики
        settings_conv_handler = ConversationHandler(...)
        application.add_handler(settings_conv_handler)
        application.add_handler(CommandHandler("start", start, filters=user_filter))
        # ... (все остальные обработчики как в прошлой версии) ...
        # Добавляем обработчик для YouTube ссылок
        application.add_handler(MessageHandler(filters.Regex(YOUTUBE_URL_PATTERN) & user_filter, handle_youtube_link))
    
    # --- Запуск ---
    if BOT_MODE == "worker":
        await application.run_polling()
    else: # Режим "main"
        port = int(os.environ.get('PORT', 8000))
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        try:
            async with application:
                await application.start()
                await application.updater.start_polling()
                await server.serve()
                await application.updater.stop()
                await application.stop()
        except Exception as e:
            logger.error(f"Произошла критическая ошибка: {e}", exc_info=True)

# --- 7. ТОЧКА ВХОДА ---
if __name__ == "__main__":
    asyncio.run(main())