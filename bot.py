# bot_final_v2.py

import os
import logging
from datetime import datetime, timedelta
import zipfile
import io
from typing import List, Dict, Any, Optional, Tuple
import asyncio

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from openpyxl import Workbook
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter

# --- Глобальные переменные и константы ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Вы можете поменять это значение на 60 или любое другое
EXPIRATION_THRESHOLD_DAYS = 30 
RED_FILL = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")
ORANGE_FILL = PatternFill(start_color="FFDDAA", end_color="FFDDAA", fill_type="solid")
GREEN_FILL = PatternFill(start_color="CCFFCC", end_color="CCFFCC", fill_type="solid")
EXCEL_HEADERS: Tuple[str, ...] = ("ФИО", "Учреждение", "Серийный номер", "Действителен с", "Действителен до", "Осталось дней")
ALLOWED_EXTENSIONS: Tuple[str, ...] = ('.cer', '.crt', '.pem', '.der')


# --- Вспомогательные функции ---

def get_certificate_info(cert_bytes: bytes) -> Optional[Dict[str, Any]]:
    try:
        try:
            cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())
        except ValueError:
            cert = x509.load_der_x509_certificate(cert_bytes, default_backend())
        try:
            subject_common_name = cert.subject.get_attributes_for_oid(x509.OID_COMMON_NAME)[0].value
        except IndexError:
            subject_common_name = "Неизвестно"
        try:
            organization_name = cert.subject.get_attributes_for_oid(x509.OID_ORGANIZATION_NAME)[0].value
        except IndexError:
            organization_name = "Неизвестно"
        serial_number = f"{cert.serial_number:X}"
        valid_from = cert.not_valid_before.date()
        valid_until = cert.not_valid_after.date()
        days_left = (valid_until - datetime.now().date()).days
        return {"ФИО": subject_common_name, "Учреждение": organization_name, "Серийный номер": serial_number, "Действителен с": valid_from, "Действителен до": valid_until, "Осталось дней": days_left}
    except Exception as e:
        logger.error(f"Ошибка при парсинге сертификата: {e}")
        return None

def create_excel_report(cert_data_list: List[Dict[str, Any]]) -> io.BytesIO:
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
        elif 0 <= days_left <= EXPIRATION_THRESHOLD_DAYS: fill_color = ORANGE_FILL
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

# <<< ИЗМЕНЕНИЕ: Функция теперь не показывает просроченные сертификаты
def generate_summary_message(cert_data_list: List[Dict[str, Any]]) -> str:
    """
    Генерирует сводное сообщение ТОЛЬКО о скоро истекающих сертификатах.
    Просроченные сертификаты игнорируются в сообщении.
    """
    expiring_soon_certs = []
    for cert_data in cert_data_list:
        days_left = cert_data["Осталось дней"]
        # Собираем только те, что скоро истекают (0 <= дней <= порог)
        if 0 <= days_left <= EXPIRATION_THRESHOLD_DAYS:
            expiring_soon_certs.append(f"👤 {cert_data['ФИО']} — {cert_data['Действителен до'].strftime('%d.%m.%Y')} (осталось {days_left} дн.)")
    
    # Если есть такие сертификаты, формируем сообщение
    if expiring_soon_certs:
        message_parts = [
            f"⚠️ Скоро истекают ({EXPIRATION_THRESHOLD_DAYS} дней):",
            *expiring_soon_certs
        ]
        return "\n".join(message_parts)
    # Если нет, возвращаем другое сообщение
    else:
        return "✅ Сертификатов, истекающих в ближайшее время, не найдено."


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


# --- Обработчики команд и сообщений ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    keyboard = [
        ["📜 Сертификат", "📄 Заявка АКЦ"],
        ["⚙️ Настройки", "❓ Помощь"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    start_message = (
        f"Привет, {user.mention_html()}! 👋\n\n"
        "Я бот для анализа цифровых сертификатов. Мои основные функции:\n"
        "– Анализ файлов .cer, .crt, .pem, .der\n"
        "– Обработка ZIP-архивов с сертификатами\n"
        "– Создание Excel-отчета со сроками действия\n\n"
        "Выберите действие в меню ниже:"
    )
    await update.message.reply_html(start_message, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Чтобы получить отчет, нажмите на кнопку '📜 Сертификат' и отправьте мне файл(ы) в формате ({', '.join(ALLOWED_EXTENSIONS)}) или ZIP-архив.")

async def request_certificate_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пожалуйста, отправьте мне файл(ы) сертификатов "
        f"({', '.join(ALLOWED_EXTENSIONS)}) или ZIP-архив с ними.\n"
        "Я проанализирую их и пришлю вам отчет."
    )

async def settings_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Этот раздел находится в разработке. Скоро здесь появятся новые функции!")

async def acc_finance_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Пользователь {update.effective_user.id} нажал на кнопку-заглушку 'Заявка АКЦ'.")
    message_text = (
        "📈 **Функция 'Заявка АКЦ-Финансы' в разработке.**\n\n"
        "Скоро здесь появится возможность автоматически формировать заявку "
        "на регистрацию (или изменение данных) пользователя в ЦИТП для прикрепления вашего сертификата.\n\n"
        "Следите за обновлениями!"
    )
    await update.message.reply_html(message_text)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.message.document
    file_name = document.file_name
    logger.info(f"Получен файл: {file_name} от пользователя {update.effective_user.id}")
    await update.message.reply_text("Анализирую сертификат(ы), пожалуйста подождите...")
    try:
        file_object = await context.bot.get_file(document.file_id)
        file_buffer = io.BytesIO()
        await file_object.download_to_memory(file_buffer)
        file_buffer.seek(0)
        all_certs_data = _process_file_content(file_buffer.read(), file_name)
        if not all_certs_data:
            await update.message.reply_text("Не удалось найти или проанализировать сертификаты в отправленном файле/архиве. Убедитесь, что формат файлов корректен.")
            return
        excel_buffer = create_excel_report(all_certs_data)
        summary_message = generate_summary_message(all_certs_data)
        await update.message.reply_text(summary_message)
        await update.message.reply_document(document=excel_buffer, filename="Сертификаты_отчет.xlsx")
        logger.info(f"Отчет по сертификатам отправлен пользователю {update.effective_user.id}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при обработке документа: {e}", exc_info=True)
        await update.message.reply_text(f"Произошла непредвиденная ошибка: {e}. Попробуйте еще раз.")

async def handle_wrong_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    file_name = update.message.document.file_name
    logger.info(f"Пользователь {user_id} отправил файл неверного формата: {file_name}")
    allowed_ext_str = ", ".join(ALLOWED_EXTENSIONS)
    await update.message.reply_text(
        f"❌ Неверный формат файла.\n\n"
        f"Я принимаю только файлы с расширениями: {allowed_ext_str}, а также .zip архивы."
    )


# --- Основная функция ---

async def main() -> None:
    """Настраивает и запускает бота с использованием правильного асинхронного контекста."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Токен Telegram бота не найден.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрация всех обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("❓ Помощь"), help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("📜 Сертификат"), request_certificate_files))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("⚙️ Настройки"), settings_placeholder))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("📄 Заявка АКЦ"), acc_finance_placeholder))
    allowed_extensions_filter = (
        filters.Document.FileExtension("zip") | filters.Document.FileExtension("cer") |
        filters.Document.FileExtension("crt") | filters.Document.FileExtension("pem") |
        filters.Document.FileExtension("der")
    )
    application.add_handler(MessageHandler(allowed_extensions_filter & ~filters.COMMAND, handle_document))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_wrong_document))

    try:
        logger.info("Бот запускается...")
        async with application:
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("Бот успешно запущен и работает.")
            await asyncio.Future()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот останавливается...")
        logger.info("Бот успешно остановлен.")
    except Exception as e:
        logger.error(f"Произошла критическая ошибка: {e}", exc_info=True)


# --- Точка входа для запуска скрипта ---

if __name__ == "__main__":
    asyncio.run(main())