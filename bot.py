# =================================================================================
#   ФИНАЛЬНАЯ ВЕРСИЯ БОТА (V34 - КОРРЕКТНЫЙ ЗАПУСК В ОБОИХ РЕЖИМАХ)
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
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
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
BOT_MODE = os.environ.get("BOT_MODE", "main")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_USER_IDS: Set[int] = {96238783}
user_filter = filters.User(user_id=ALLOWED_USER_IDS)

MAX_FILE_SIZE = 20 * 1024 * 1024
EXPIRATION_THRESHOLD_DAYS = 30
RED_FILL = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")
ORANGE_FILL = PatternFill(start_color="FFDDAA", end_color="FFDDAA", fill_type="solid")
GREEN_FILL = PatternFill(start_color="CCFFCC", end_color="CCFFCC", fill_type="solid")
EXCEL_HEADERS: Tuple[str, ...] = ("ФИО", "Учреждение", "Серийный номер", "Действителен с", "Действителен до", "Осталось дней")
ALLOWED_EXTENSIONS: Tuple[str, ...] = ('.cer', '.crt', '.pem', '.der')
CHOOSING_ACTION, TYPING_DAYS = range(2)


# --- 3. ВЕБ-СЕРВЕР FASTAPI ---
app = FastAPI(docs_url=None, redoc_url=None)

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "bot is running"}


# --- 4. РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Не удалось подключиться к базе данных: {e}", exc_info=True)
        return None

def init_database():
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute('CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, threshold INTEGER NOT NULL)')
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
        logger.info("База данных PostgreSQL успешно инициализирована.")
    except psycopg2.Error as e:
        logger.error(f"Ошибка при инициализации таблиц: {e}", exc_info=True)
    finally:
        if conn: conn.close()

def save_user_threshold(user_id: int, threshold: int):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_settings (user_id, threshold) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET threshold = EXCLUDED.threshold;",
                (user_id, threshold)
            )
        conn.commit()
        logger.info(f"Порог {threshold} для пользователя {user_id} сохранен в БД.")
    except psycopg2.Error as e:
        logger.error(f"Ошибка при сохранении порога: {e}", exc_info=True)
    finally:
        if conn: conn.close()

def load_user_threshold(user_id: int) -> Optional[int]:
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT threshold FROM user_settings WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except psycopg2.Error as e:
        logger.error(f"Ошибка при загрузке порога: {e}", exc_info=True)
        return None
    finally:
        if conn: conn.close()

async def get_user_threshold(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'threshold' in context.user_data:
        return context.user_data['threshold']
    threshold_from_db = load_user_threshold(user_id)
    if threshold_from_db is not None:
        context.user_data['threshold'] = threshold_from_db
        return threshold_from_db
    return EXPIRATION_THRESHOLD_DAYS

def create_download_task(user_id: int, youtube_url: str) -> Optional[str]:
    conn = get_db_connection()
    if not conn: return None
    task_id = uuid.uuid4()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO download_tasks (task_id, user_id, youtube_url) VALUES (%s, %s, %s)",(task_id, user_id, youtube_url))
        conn.commit()
        logger.info(f"Создано задание {task_id} для пользователя {user_id}")
        return str(task_id)
    except psycopg2.Error as e:
        logger.error(f"Ошибка при создании задания: {e}", exc_info=True)
        return None
    finally:
        if conn: conn.close()

def get_task_details(task_id: str) -> Optional[Dict]:
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, youtube_url FROM download_tasks WHERE task_id = %s AND status = 'new'", (task_id,))
            result = cursor.fetchone()
        if result:
            return {"user_id": result[0], "youtube_url": result[1]}
        return None
    except psycopg2.Error as e:
        logger.error(f"Ошибка при получении задания {task_id}: {e}", exc_info=True)
        return None
    finally:
        if conn: conn.close()

def update_task_status(task_id: str, status: str):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE download_tasks SET status = %s WHERE task_id = %s", (status, task_id))
        conn.commit()
    except psycopg2.Error as e:
        logger.error(f"Ошибка при обновлении статуса задания {task_id}: {e}", exc_info=True)
    finally:
        if conn: conn.close()

# --- 5. ОБРАБОТЧИКИ И ЛОГИКА ---
YOUTUBE_URL_PATTERN = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'

async def handle_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text
    task_id = create_download_task(update.effective_user.id, url)
    if task_id:
        keyboard = [[InlineKeyboardButton("▶️ Начать скачивание", callback_data=f"download_{task_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("✅ Задание на скачивание создано.\n\nЗапустите 'рабочего' бота на вашем ПК и нажмите кнопку ниже.", reply_markup=reply_markup)
    else:
        await update.message.reply_text("❌ Не удалось создать задание на скачивание.")

async def process_download_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    task_id = query.data.split('_')[1]

    task_details = get_task_details(task_id)
    if not task_details:
        await query.edit_message_text("❌ Задание не найдено или уже выполнено."); return

    await query.edit_message_text("Начинаю загрузку видео, это может занять время...");
    update_task_status(task_id, 'processing')
    
    ydl_opts = {'format': 'best[height<=1080][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]','outtmpl': f'{task_id}.%(ext)s', 'quiet': True}
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(task_details['youtube_url'], download=True)
            video_filename = ydl.prepare_filename(info)

        await query.edit_message_text("Видео скачано. Отправляю...");
        await context.bot.send_video(chat_id=task_details['user_id'], video=open(video_filename, 'rb'), supports_streaming=True, read_timeout=120, write_timeout=120)
        
        os.remove(video_filename)
        await query.message.delete()
        update_task_status(task_id, 'completed')

    except Exception as e:
        logger.error(f"Ошибка при скачивании/отправке видео для задания {task_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ Не удалось скачать или отправить видео.")
        update_task_status(task_id, 'failed')

# ... (остальные обработчики без изменений) ...

# --- 6. ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
async def main() -> None:
    init_database()
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        logger.error("Не найден токен или URL базы данных."); return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # --- Регистрация обработчиков ---
    if BOT_MODE == "worker":
        application.add_handler(CallbackQueryHandler(process_download_task, pattern='^download_'))
        logger.info("Бот запущен в режиме 'Рабочий'. Ожидает заданий на скачивание...")
    else: # Режим "main"
        settings_conv_handler = ConversationHandler(...)
        application.add_handler(settings_conv_handler)
        application.add_handler(CommandHandler("start", start, filters=user_filter))
        application.add_handler(MessageHandler(filters.Regex(YOUTUBE_URL_PATTERN) & user_filter, handle_youtube_link))
        # ... (и другие обработчики для 'main')
    
    # --- Запуск ---
    try:
        if BOT_MODE == "worker":
            async with application:
                await application.start()
                await application.updater.start_polling()
                logger.info("Бот-поллер для 'рабочего' запущен.")
                await asyncio.Future()
        else: # Режим "main"
            port = int(os.environ.get('PORT', 8000))
            config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
            server = uvicorn.Server(config)
            logger.info("Запускаю бота (polling) и веб-сервер (uvicorn)...")
            async with application:
                await application.start()
                await application.updater.start_polling()
                await server.serve()
                await application.updater.stop()
                await application.stop()
    except Exception as e:
        logger.error(f"Произошла критическая ошибка при запуске: {e}", exc_info=True)


# --- 7. ТОЧКА ВХОДА ---
if __name__ == "__main__":
    asyncio.run(main())