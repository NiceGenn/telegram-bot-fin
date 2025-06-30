# =================================================================================
#    ФИНАЛЬНАЯ ВЕРСИЯ БОТА (V22 - С БАЗОЙ ДАННЫХ SQLITE)
# =================================================================================

# --- 1. ИМПОРТЫ ---
import os
import logging
from datetime import datetime
import zipfile
import io
from typing import List, Dict, Any, Optional, Tuple, Set
import asyncio
import sqlite3 # <<< НОВОЕ: Импорт для работы с SQLite

import uvicorn
from fastapi import FastAPI
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ALLOWED_USER_IDS: Set[int] = {96238783}
user_filter = filters.User(user_id=ALLOWED_USER_IDS)

# Глобальные константы
DB_NAME = "bot_storage.db" # <<< НОВОЕ: Имя файла базы данных
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


# --- 4. РАБОТА С БАЗОЙ ДАННЫХ SQLITE ---

def init_database():
    """Инициализирует базу данных и создает таблицу, если она не существует."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Создаем таблицу для хранения настроек порога для каждого пользователя
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                threshold INTEGER NOT NULL
            )
        ''')
        conn.commit()
        conn.close()
        logger.info(f"База данных '{DB_NAME}' успешно инициализирована.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}", exc_info=True)

def save_user_threshold(user_id: int, threshold: int):
    """Сохраняет или обновляет порог оповещения для пользователя в БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # INSERT OR REPLACE вставит новую запись или заменит существующую, если user_id уже есть
        cursor.execute("INSERT OR REPLACE INTO user_settings (user_id, threshold) VALUES (?, ?)", (user_id, threshold))
        conn.commit()
        conn.close()
        logger.info(f"Порог {threshold} для пользователя {user_id} сохранен в БД.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при сохранении порога для пользователя {user_id}: {e}", exc_info=True)

def load_user_threshold(user_id: int) -> Optional[int]:
    """Загружает порог оповещения для пользователя из БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT threshold FROM user_settings WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        conn.close()
        if result:
            logger.info(f"Порог {result[0]} для пользователя {user_id} загружен из БД.")
            return result[0]
        return None
    except sqlite3.Error as e:
        logger.error(f"Ошибка при загрузке порога для пользователя {user_id}: {e}", exc_info=True)
        return None

async def get_user_threshold(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Получает порог пользователя.
    Порядок приоритета: временное хранилище -> база данных -> значение по умолчанию.
    """
    # 1. Проверяем временное хранилище (самый быстрый доступ)
    if 'threshold' in context.user_data:
        return context.user_data['threshold']

    # 2. Если нет, загружаем из БД
    threshold_from_db = load_user_threshold(user_id)
    if threshold_from_db is not None:
        context.user_data['threshold'] = threshold_from_db # Сохраняем в кеш на время сессии
        return threshold_from_db

    # 3. Если и в БД нет, используем значение по умолчанию
    return EXPIRATION_THRESHOLD_DAYS


# --- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ОБРАБОТЧИКИ ---

# ... (create_excel_report, generate_summary_message, и т.д. без изменений,
#      так как они уже принимают `user_threshold` как аргумент)

# <<< ИЗМЕНЕНИЕ: Используем новую функцию для получения порога >>>
async def settings_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало диалога настроек."""
    user_id = update.effective_user.id
    current_threshold = await get_user_threshold(user_id, context)
    
    keyboard = [
        [InlineKeyboardButton("Изменить порог", callback_data='change_threshold')],
        [InlineKeyboardButton("Назад", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚙️ **Настройки оповещений**\n\n"
        f"Текущий порог для предупреждения: **{current_threshold}** дней.\n\n"
        "Эта настройка сохраняется даже после перезапуска бота.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return CHOOSING_ACTION

# <<< ИЗМЕНЕНИЕ: Сохраняем настройку в БД >>>
async def set_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Устанавливает новый порог и сохраняет его в БД."""
    user_id = update.effective_user.id
    try:
        new_threshold = int(update.message.text)
        if new_threshold <= 0:
            await update.message.reply_text("❌ Пожалуйста, введите положительное число.")
            return TYPING_DAYS

        context.user_data['threshold'] = new_threshold # Обновляем во временном хранилище
        save_user_threshold(user_id, new_threshold) # Сохраняем в БД
        
        await update.message.reply_text(f"✅ Порог оповещения успешно изменен и сохранен: **{new_threshold}** дней.", parse_mode='Markdown')
        
    except (ValueError):
        await update.message.reply_text("❌ Это не похоже на число. Пожалуйста, отправьте число, например: 60")
        return TYPING_DAYS

    return ConversationHandler.END

# <<< ИЗМЕНЕНИЕ: Используем новую функцию для получения порога >>>
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.message.document
    if document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ Файл слишком большой.\nМаксимальный разрешенный размер: {MAX_FILE_SIZE / 1024 / 1024:.0f} МБ.")
        return

    user_id = update.effective_user.id
    user_threshold = await get_user_threshold(user_id, context)

    file_name = document.file_name
    logger.info(f"Получен файл: {file_name} от пользователя {user_id}")
    await update.message.reply_text("Анализирую сертификат(ы), пожалуйста подождите...")
    try:
        file_object = await context.bot.get_file(document.file_id)
        file_buffer = io.BytesIO()
        await file_object.download_to_memory(file_buffer)
        file_buffer.seek(0)
        all_certs_data = _process_file_content(file_buffer.read(), file_name)
        if not all_certs_data:
            await update.message.reply_text("Не удалось найти или проанализировать сертификаты.")
            return
        
        excel_buffer = create_excel_report(all_certs_data, user_threshold)
        summary_message = generate_summary_message(all_certs_data, user_threshold)
        
        await update.message.reply_text(summary_message)
        await update.message.reply_document(document=excel_buffer, filename="Сертификаты_отчет.xlsx")
        logger.info(f"Отчет по сертификатам отправлен.")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при обработке документа: {e}", exc_info=True)
        await update.message.reply_text(f"Произошла непредвиденная ошибка.")

# ... (остальные функции-обработчики без изменений)


# --- 6. ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
async def main() -> None:
    # <<< НОВОЕ: Инициализируем БД перед запуском >>>
    init_database()

    if not TELEGRAM_BOT_TOKEN:
        logger.error("Токен Telegram бота не найден.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ... (регистрация всех обработчиков без изменений)

    port = int(os.environ.get('PORT', 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    try:
        logger.info("Запускаю бота (polling) и веб-сервер (uvicorn)...")
        async with application:
            await application.start()
            await application.updater.start_polling()
            await server.serve()
            await application.updater.stop()
            await application.stop()
    except Exception as e:
        logger.error(f"Произошла критическая ошибка при запуске: {e}", exc_info=True)


# --- 7. ТОЧКА ВХОДА ДЛЯ ЗАПУСКА СКРИПТА ---
if __name__ == "__main__":
    # Код всех функций, которые не менялись, скрыт для краткости
    def create_excel_report(cert_data_list: List[Dict[str, Any]], user_threshold: int) -> io.BytesIO:
        wb = Workbook()
        ws = wb.active
        ws.title = "Отчет по сертификатам"
        ws.append(list(EXCEL_HEADERS))
        sorted_cert_data = sorted(cert_data_list, key=lambda x: x["Действителен до"])
        for cert_data in sorted_cert_data:
            row = [cert_data["ФИО"], cert_data["Учреждение"], cert_data["Серийный номер"], cert_data["Действителен с"].strftime("%d.%m.%Y"), cert_data["Действителен до"].strftime("%d.%m.%Y"), cert_data["Осталось дней"]]
            ws.append(row)
            last_row = ws.max_row
            days_left = cert_data["Осталось дней"]
            fill_color = None
            if days_left < 0: fill_color = RED_FILL
            elif 0 <= days_left <= user_threshold: fill_color = ORANGE_FILL
            else: fill_color = GREEN_FILL
            if fill_color:
                for cell in ws[last_row]: cell.fill = fill_color
        for column in ws.columns:
            max_length = 0
            column_letter = get_column_letter(column[0].column)
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length: max_length = len(str(cell.value))
                except: pass
            adjusted_width = (max_length + 2)
            ws.column_dimensions[column_letter].width = adjusted_width
        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)
        return excel_buffer
    def generate_summary_message(cert_data_list: List[Dict[str, Any]], user_threshold: int) -> str:
        expiring_soon_certs = []
        for cert_data in cert_data_list:
            days_left = cert_data["Осталось дней"]
            if 0 <= days_left <= user_threshold:
                expiring_soon_certs.append(f"👤 {cert_data['ФИО']} — {cert_data['Действителен до'].strftime('%d.%m.%Y')} (осталось {days_left} дн.)")
        if expiring_soon_certs:
            message_parts = [f"⚠️ Скоро истекают ({user_threshold} дней):", *expiring_soon_certs]
            return "\n".join(message_parts)
        else:
            return "✅ Сертификатов, истекающих в ближайшее время, не найдено."
    def get_certificate_info(cert_bytes: bytes) -> Optional[Dict[str, Any]]:
        try:
            try: cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())
            except ValueError: cert = x509.load_der_x509_certificate(cert_bytes, default_backend())
            try: subject_common_name = cert.subject.get_attributes_for_oid(x509.OID_COMMON_NAME)[0].value
            except IndexError: subject_common_name = "Неизвестно"
            try: organization_name = cert.subject.get_attributes_for_oid(x509.OID_ORGANIZATION_NAME)[0].value
            except IndexError: organization_name = "Неизвестно"
            serial_number = f"{cert.serial_number:X}"
            valid_from = cert.not_valid_before.date()
            valid_until = cert.not_valid_after.date()
            days_left = (valid_until - datetime.now().date()).days
            return {"ФИО": subject_common_name, "Учреждение": organization_name, "Серийный номер": serial_number, "Действителен с": valid_from, "Действителен до": valid_until, "Осталось дней": days_left}
        except Exception as e:
            logger.error(f"Ошибка при парсинге сертификата: {e}")
            return None
    def _process_file_content(file_bytes: bytes, file_name: str) -> List[Dict[str, Any]]:
        all_certs_data = []
        if file_name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes), 'r') as z:
                    for member in z.namelist():
                        if member.lower().endswith(ALLOWED_EXTENSIONS):
                            with z.open(member) as cert_file:
                                cert_info = get_certificate_info(cert_file.read())
                                if cert_info: all_certs_data.append(cert_info)
            except zipfile.BadZipFile:
                logger.error(f"Получен поврежденный ZIP-файл: {file_name}", exc_info=True)
                return []
        elif file_name.lower().endswith(ALLOWED_EXTENSIONS):
            cert_info = get_certificate_info(file_bytes)
            if cert_info: all_certs_data.append(cert_info)
        return all_certs_data
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        keyboard = [["📜 Сертификат", "📄 Заявка АКЦ"], ["⚙️ Настройки", "❓ Помощь"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        start_message = (f"Привет, {user.mention_html()}! 👋\n\n"
                         "Я бот для анализа цифровых сертификатов. Мои основные функции:\n"
                         "– Анализ файлов .cer, .crt, .pem, .der\n"
                         "– Обработка ZIP-архивов с сертификатами\n"
                         "– Создание Excel-отчета со сроками действия\n\n"
                         "Выберите действие в меню ниже:")
        await update.message.reply_html(start_message, reply_markup=reply_markup)
    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"Чтобы получить отчет, нажмите на кнопку '📜 Сертификат' и отправьте мне файл(ы) в формате ({', '.join(ALLOWED_EXTENSIONS)}) или ZIP-архив.")
    async def get_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        await update.message.reply_text(f"Ваш User ID: `{user_id}`\n\nЭтот ID уже добавлен в список разрешенных.", parse_mode='MarkdownV2')
    async def prompt_for_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text="Пожалуйста, отправьте новое число дней для порога оповещения (например, 60).")
        return TYPING_DAYS
    async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text="Настройки закрыты.")
        return ConversationHandler.END
    async def handle_wrong_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        document = update.message.document
        if document.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(f"❌ Файл слишком большой.\nМаксимальный разрешенный размер: {MAX_FILE_SIZE / 1024 / 1024:.0f} МБ.")
            return
        user_id = update.effective_user.id
        file_name = document.file_name
        logger.info(f"Пользователь {user_id} отправил файл неверного формата: {file_name}")
        allowed_ext_str = ", ".join(ALLOWED_EXTENSIONS)
        await update.message.reply_text(f"❌ Неверный формат файла.\n\n"
                                         f"Я принимаю только файлы с расширениями: {allowed_ext_str}, а также .zip архивы.")
    async def request_certificate_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Пожалуйста, отправьте мне файл(ы) сертификатов "
                                         f"({', '.join(ALLOWED_EXTENSIONS)}) или ZIP-архив с ними.\n"
                                         "Я проанализирую их и пришлю вам отчет.")
    async def acc_finance_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"Пользователь {update.effective_user.id} нажал на кнопку-заглушку 'Заявка АКЦ'.")
        message_text = ("📈 **Функция 'Заявка АКЦ-Финансы' в разработке.**\n\n"
                        "Скоро здесь появится возможность автоматически формировать заявку "
                        "на регистрацию (или изменение данных) пользователя в ЦИТП для прикрепления вашего сертификата.\n\n"
                        "Следите за обновлениями!")
        await update.message.reply_html(message_text)
    
    # Регистрация обработчиков в main
    async def main_setup(application):
        settings_conv_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^⚙️ Настройки$') & user_filter, settings_entry)],
            states={
                CHOOSING_ACTION: [
                    CallbackQueryHandler(prompt_for_days, pattern='^change_threshold$'),
                    CallbackQueryHandler(end_conversation, pattern='^back_to_main$'),
                ],
                TYPING_DAYS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, set_days)
                ],
            },
            fallbacks=[CommandHandler('start', start)],
        )
        application.add_handler(CommandHandler("my_id", get_my_id))
        application.add_handler(CommandHandler("start", start, filters=user_filter))
        application.add_handler(CommandHandler("help", help_command, filters=user_filter))
        application.add_handler(settings_conv_handler)
        application.add_handler(MessageHandler(filters.Regex('^❓ Помощь$') & user_filter, help_command))
        application.add_handler(MessageHandler(filters.Regex('^📜 Сертификат$') & user_filter, request_certificate_files))
        application.add_handler(MessageHandler(filters.Regex('^📄 Заявка АКЦ$') & user_filter, acc_finance_placeholder))
        allowed_extensions_filter = (
            filters.Document.FileExtension("zip") |
            filters.Document.FileExtension("cer") |
            filters.Document.FileExtension("crt") |
            filters.Document.FileExtension("pem") |
            filters.Document.FileExtension("der")
        )
        application.add_handler(MessageHandler(allowed_extensions_filter & ~filters.COMMAND & user_filter, handle_document))
        application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, handle_wrong_document))

    async def main():
        init_database()
        if not TELEGRAM_BOT_TOKEN:
            logger.error("Токен Telegram бота не найден.")
            return
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        await main_setup(application)
        port = int(os.environ.get('PORT', 8000))
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        try:
            logger.info("Запускаю бота (polling) и веб-сервер (uvicorn)...")
            async with application:
                await application.start()
                await application.updater.start_polling()
                await server.serve()
                await application.updater.stop()
                await application.stop()
        except Exception as e:
            logger.error(f"Произошла критическая ошибка при запуске: {e}", exc_info=True)

    asyncio.run(main())