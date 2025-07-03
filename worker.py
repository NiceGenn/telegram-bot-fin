# =================================================================================
#   ФАЙЛ: worker.py (V2 - ОТКАЗОУСТОЙЧИВЫЙ)
# =================================================================================

# --- 1. ИМПОРТЫ ---
import os
import logging
import asyncio
import psycopg2
import yt_dlp
import telegram
from dotenv import load_dotenv

# --- 2. НАСТРОЙКА И КОНСТАНТЫ ---
load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 3. ФУНКЦИИ ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ---

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Worker: Не удалось подключиться к БД: {e}")
        return None

def get_new_task_to_download():
    """Ищет одно задание со статусом 'new' для скачивания."""
    conn = get_db_connection()
    if not conn: return None
    task = None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT task_id, user_id, youtube_url FROM download_tasks WHERE status = 'new' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")
            task = cursor.fetchone()
            if task:
                cursor.execute("UPDATE download_tasks SET status = 'downloading' WHERE task_id = %s", (task[0],))
            conn.commit()
    finally:
        if conn: conn.close()
    return task

def get_task_to_send():
    """Ищет одно задание со статусом 'downloaded' для отправки."""
    conn = get_db_connection()
    if not conn: return None
    task = None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT task_id, user_id, local_filepath FROM download_tasks WHERE status = 'downloaded' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")
            task = cursor.fetchone()
            if task:
                cursor.execute("UPDATE download_tasks SET status = 'sending' WHERE task_id = %s", (task[0],))
            conn.commit()
    finally:
        if conn: conn.close()
    return task

def update_task_after_download(task_id, filepath, status):
    """Обновляет задание после скачивания, сохраняя путь к файлу."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE download_tasks SET local_filepath = %s, status = %s WHERE task_id = %s", (filepath, status, task_id))
        conn.commit()
    finally:
        if conn: conn.close()

def update_task_status(task_id, status: str):
    """Обновляет только статус задания."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE download_tasks SET status = %s WHERE task_id = %s", (status, task_id))
        conn.commit()
    finally:
        if conn: conn.close()


# --- 4. ОСНОВНАЯ ЛОГИКА РАБОЧЕГО ---

async def download_new_videos(bot):
    """Фаза 1: Ищет и скачивает новые видео."""
    task = get_new_task_to_download()
    if not task:
        return False # Сообщаем основному циклу, что работы не было

    task_id, user_id, youtube_url = task
    logger.info(f"Начинаю скачивание для задания {task_id}")
    await bot.send_message(chat_id=user_id, text=f"Начинаю скачивание видео по ссылке:\n{youtube_url}")
    
    ydl_opts = {'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', 'outtmpl': f'{task_id}.%(ext)s', 'quiet': True}
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            video_filename = ydl.prepare_filename(info)
        
        update_task_after_download(task_id, video_filename, 'downloaded')
        logger.info(f"Видео {video_filename} скачано и готово к отправке.")
        await bot.send_message(chat_id=user_id, text="✅ Видео скачано. Ставлю в очередь на отправку.")
    except Exception as e:
        logger.error(f"Ошибка при скачивании задания {task_id}: {e}", exc_info=True)
        update_task_status(task_id, 'failed')
        await bot.send_message(chat_id=user_id, text=f"❌ Не удалось скачать видео по ссылке:\n{youtube_url}")
    
    return True # Сообщаем, что работа была

async def send_downloaded_videos(bot):
    """Фаза 2: Ищет и отправляет уже скачанные видео."""
    task = get_task_to_send()
    if not task:
        return False # Работы не было

    task_id, user_id, video_filename = task
    logger.info(f"Начинаю отправку файла {video_filename} для задания {task_id}")

    if not os.path.exists(video_filename):
        logger.error(f"Файл {video_filename} не найден на диске для задания {task_id}!")
        update_task_status(task_id, 'failed')
        await bot.send_message(chat_id=user_id, text=f"❌ Ошибка: скачанный файл для вашего задания был утерян.")
        return True

    try:
        await bot.send_message(chat_id=user_id, text=f"Отправляю вам скачанное видео...")
        with open(video_filename, 'rb') as video_file:
            await bot.send_video(chat_id=user_id, video=video_file, supports_streaming=True, read_timeout=120, write_timeout=120)
        
        os.remove(video_filename)
        update_task_status(task_id, 'completed')
        logger.info(f"Задание {task_id} успешно выполнено и файл удален.")
    except telegram.error.TimedOut:
        logger.warning(f"Тайм-аут при отправке задания {task_id}. Попробую снова позже.")
        update_task_status(task_id, 'downloaded') # Возвращаем статус для повторной попытки
    except Exception as e:
        logger.error(f"Ошибка при отправке задания {task_id}: {e}", exc_info=True)
        update_task_status(task_id, 'failed')
        await bot.send_message(chat_id=user_id, text=f"❌ Произошла ошибка при отправке видео. Попробуйте создать задание заново.")
        
    return True # Работа была

async def main_worker():
    """Главный асинхронный цикл работы."""
    logger.info("✅ Отказоустойчивый рабочий (Worker) запущен.")
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

    while True:
        try:
            # Пытаемся выполнить по одному заданию каждого типа за итерацию
            downloaded = await download_new_videos(bot)
            sent = await send_downloaded_videos(bot)

            # Если никакой работы не было, немного ждем
            if not downloaded and not sent:
                await asyncio.sleep(5)

        except KeyboardInterrupt:
            logger.info("Получен сигнал на остановку. Завершаю работу..."); break
        except Exception as e:
            logger.error(f"Критическая ошибка в главном цикле рабочего: {e}", exc_info=True)
            await asyncio.sleep(30)


# --- 5. ТОЧКА ВХОДА ДЛЯ ЗАПУСКА СКРИПТА ---
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        print("ОШИБКА: Убедитесь, что TELEGRAM_BOT_TOKEN и DATABASE_URL заданы в .env файле.")
    else:
        asyncio.run(main_worker())
