# =================================================================================
#   ФАЙЛ: worker.py (V1 - РАБОЧИЙ-ИСПОЛНИТЕЛЬ)
# =================================================================================

# --- 1. ИМПОРТЫ ---
import os
import logging
import time
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
    """Устанавливает соединение с базой данных PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Worker: Не удалось подключиться к БД: {e}")
        return None

def get_new_task():
    """Ищет одно новое задание и сразу блокирует его для других воркеров."""
    conn = get_db_connection()
    if not conn: return None
    task = None
    try:
        with conn.cursor() as cursor:
            # FOR UPDATE SKIP LOCKED - продвинутая возможность PostgreSQL,
            # которая позволяет нескольким воркерам не брать одно и то же задание.
            cursor.execute(
                "SELECT task_id, user_id, youtube_url FROM download_tasks "
                "WHERE status = 'new' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            task = cursor.fetchone()
            if task:
                # Сразу меняем статус, чтобы другие воркеры его не взяли
                cursor.execute("UPDATE download_tasks SET status = 'processing' WHERE task_id = %s", (task[0],))
            conn.commit()
    except Exception as e:
        logger.error(f"Worker: Ошибка при получении задания из БД: {e}")
    finally:
        if conn: conn.close()
    return task

def update_task_status(task_id, status: str):
    """Обновляет статус задания в БД."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE download_tasks SET status = %s WHERE task_id = %s", (status, task_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Worker: Ошибка при обновлении статуса задания {task_id}: {e}")
    finally:
        if conn: conn.close()


# --- 4. ОСНОВНАЯ ЛОГИКА РАБОЧЕГО ---

def main_worker():
    """Главный цикл работы, который ищет и выполняет задания."""
    logger.info("✅ Рабочий (Worker) запущен. Ищу новые задания...")
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

    while True:
        try:
            task = get_new_task()
            if task:
                task_id, user_id, youtube_url = task
                logger.info(f"Начинаю обработку задания {task_id} для пользователя {user_id}")
                
                # Настройки скачивания yt-dlp
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': f'{task_id}.%(ext)s', # Имя файла будет равно ID задания
                    'quiet': True,
                }
                
                try:
                    # Скачиваем видео
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(youtube_url, download=True)
                        video_filename = ydl.prepare_filename(info)
                    
                    logger.info(f"Видео {video_filename} скачано. Отправляю...")
                    bot.send_message(chat_id=user_id, text=f"Видео по ссылке {youtube_url} скачано, отправляю файл...")

                    # Отправляем видео пользователю
                    with open(video_filename, 'rb') as video_file:
                        bot.send_video(
                            chat_id=user_id, 
                            video=video_file, 
                            supports_streaming=True, 
                            read_timeout=120, # Увеличиваем таймауты для больших файлов
                            write_timeout=120
                        )
                    
                    # Удаляем временный видеофайл
                    os.remove(video_filename)
                    update_task_status(task_id, 'completed')
                    logger.info(f"Задание {task_id} успешно выполнено.")

                except Exception as e:
                    logger.error(f"Ошибка при выполнении задания {task_id}: {e}", exc_info=True)
                    update_task_status(task_id, 'failed')
                    bot.send_message(chat_id=user_id, text=f"❌ Не удалось обработать ваше видео по ссылке: {youtube_url}")
            else:
                # Если заданий нет, ждем 10 секунд перед следующей проверкой
                # logger.info("Новых заданий нет, жду 10 секунд...") # Можно раскомментировать для отладки
                time.sleep(10)

        except KeyboardInterrupt:
            logger.info("Получен сигнал на остановку. Завершаю работу...")
            break
        except Exception as e:
            logger.error(f"Критическая ошибка в главном цикле рабочего: {e}", exc_info=True)
            time.sleep(30) # В случае серьезной ошибки ждем дольше


# --- 5. ТОЧКА ВХОДА ДЛЯ ЗАПУСКА СКРИПТА ---

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        print("ОШИБКА: Убедитесь, что TELEGRAM_BOT_TOKEN и DATABASE_URL заданы в .env файле.")
    else:
        main_worker()