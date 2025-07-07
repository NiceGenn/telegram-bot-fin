# =================================================================================
#  ФАЙЛ: bot.py (V4.9 - С РАЗГРАНИЧЕНИЕМ ДОСТУПА)
# =================================================================================

# --- 1. ИМПОРТЫ ---
import os
import logging
import asyncio
from datetime import datetime
import zipfile
import io
from typing import List, Dict, Any, Optional, Tuple, Set
import psycopg2
import yt_dlp
import telegram
import uuid
import time
import docx
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from telegram import Update, ReplyKeyboardMarkup, Message, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- СИСТЕМА РАЗРЕШЕНИЙ (ROLES/PERMISSIONS) ---
# Определите здесь, какие функции доступны разным пользователям.
#
# Доступные разрешения:
# 'admin'           - полный доступ ко всем функциям.
# 'cert_analysis'   - доступ к "Анализ сертификатов".
# 'akc_form'        - доступ к "Заявка АЦК".
# 'youtube'         - доступ к "Скачивание с YouTube".
# 'settings'        - доступ к "Настройки анализа сертификатов".
#
# ПРИМЕР:
USER_PERMISSIONS: Dict[int, Set[str]] = {
    # Администратор с полным доступом
    96238783: {"admin"},
    # Пользователь, который может только создавать заявки АЦК
    12345678: {"akc_form"}, 
    # Пользователь для анализа сертификатов и скачивания видео
    00000000: {"cert_analysis", "youtube"}, 
}

# Фильтр для всех пользователей, у которых есть хоть какие-то права
authorized_user_filter = filters.User(user_id=USER_PERMISSIONS.keys())

def has_permission(user_id: int, feature: str) -> bool:
    """Проверяет, есть ли у пользователя доступ к функции."""
    permissions = USER_PERMISSIONS.get(user_id, set())
    if "admin" in permissions:
        return True
    return feature in permissions

class PermissionFilter(filters.BaseFilter):
    """Кастомный фильтр для проверки разрешений пользователя."""
    def __init__(self, feature: str):
        self.feature = feature
    def filter(self, message: Message) -> bool:
        return has_permission(message.from_user.id, self.feature)

# -------------------------------------------------

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_VIDEO_SIZE_BYTES = 49 * 1024 * 1024
EXPIRATION_THRESHOLD_DAYS = 30
RED_FILL = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")
ORANGE_FILL = PatternFill(start_color="FFDDAA", end_color="FFDDAA", fill_type="solid")
GREEN_FILL = PatternFill(start_color="CCFFCC", end_color="CCFFCC", fill_type="solid")
EXCEL_HEADERS: Tuple[str, ...] = ("ФИО", "Учреждение", "Серийный номер", "Действителен с", "Действителен до", "Осталось дней")
ALLOWED_EXTENSIONS: Tuple[str, ...] = ('.cer', '.crt', '.pem', '.der')
YOUTUBE_URL_PATTERN = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'

# Состояния для диалогов
(
    CHOOSING_ACTION, TYPING_DAYS, AWAITING_YOUTUBE_LINK, CONFIRMING_DOWNLOAD,
    AKC_CONFIRM_DEFAULTS, AKC_SENDER_FIO, AKC_ORG_NAME, AKC_INN_KPP, AKC_MUNICIPALITY,
    AKC_AWAIT_CERTIFICATES, AKC_ROLE, AKC_CITP_NAME, AKC_CONFIRM_LOGINS, AKC_LOGINS, AKC_ACTION
) = range(15)


# --- 3. РАБОТА С БАЗОЙ ДАННЫХ POSTGRESQL ---
def get_db_connection():
    """Устанавливает соединение с базой данных PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Не удалось подключиться к базе данных: {e}")
        return None

def init_database():
    """Инициализирует таблицы в базе данных, если они не существуют."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute('CREATE TABLE IF NOT EXISTS user_settings (user_id BIGINT PRIMARY KEY, threshold INTEGER NOT NULL)')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS akc_sender_defaults (
                    user_id BIGINT PRIMARY KEY,
                    sender_fio TEXT NOT NULL,
                    org_name TEXT NOT NULL,
                    inn_kpp TEXT NOT NULL,
                    municipality TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS akc_login_defaults (
                    user_id BIGINT PRIMARY KEY,
                    logins TEXT NOT NULL
                )
            ''')
        conn.commit()
        logger.info("База данных PostgreSQL успешно инициализирована.")
    except Exception as e:
        logger.error(f"Ошибка при инициализации таблиц: {e}")
    finally:
        if conn: conn.close()

def save_user_threshold(user_id: int, threshold: int):
    """Сохраняет порог дней для пользователя в базу данных."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO user_settings (user_id, threshold) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET threshold = EXCLUDED.threshold;",(user_id, threshold))
        conn.commit()
    finally:
        if conn: conn.close()

def load_user_threshold(user_id: int) -> Optional[int]:
    """Загружает порог дней для пользователя из базы данных."""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT threshold FROM user_settings WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
        return result[0] if result else None
    finally:
        if conn: conn.close()

async def get_user_threshold(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает порог дней из кэша или базы данных."""
    if 'threshold' in context.user_data:
        return context.user_data['threshold']
    
    threshold_from_db = load_user_threshold(user_id)
    if threshold_from_db is not None:
        context.user_data['threshold'] = threshold_from_db
        return threshold_from_db
    
    return EXPIRATION_THRESHOLD_DAYS

def save_akc_defaults(user_id: int, form_data: dict):
    """Сохраняет данные шапки заявки АЦК как шаблон."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO akc_sender_defaults (user_id, sender_fio, org_name, inn_kpp, municipality) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET "
                "sender_fio = EXCLUDED.sender_fio, org_name = EXCLUDED.org_name, "
                "inn_kpp = EXCLUDED.inn_kpp, municipality = EXCLUDED.municipality;",
                (user_id, form_data['sender_fio'], form_data['org_name'], form_data['inn_kpp'], form_data['municipality'])
            )
        conn.commit()
        logger.info(f"Шаблон заявки для пользователя {user_id} сохранен.")
    except Exception as e:
        logger.error(f"Ошибка при сохранении шаблона заявки для {user_id}: {e}")
    finally:
        if conn: conn.close()

def load_akc_defaults(user_id: int) -> Optional[Dict[str, str]]:
    """Загружает шаблон данных для заявки АЦК."""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT sender_fio, org_name, inn_kpp, municipality FROM akc_sender_defaults WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
        if result:
            return {
                'sender_fio': result[0],
                'org_name': result[1],
                'inn_kpp': result[2],
                'municipality': result[3]
            }
        return None
    finally:
        if conn: conn.close()

def save_akc_logins(user_id: int, logins: str):
    """Сохраняет логины пользователя для заявки АЦК."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO akc_login_defaults (user_id, logins) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET logins = EXCLUDED.logins;",
                (user_id, logins)
            )
        conn.commit()
        logger.info(f"Логины для пользователя {user_id} сохранены.")
    except Exception as e:
        logger.error(f"Ошибка при сохранении логинов для {user_id}: {e}")
    finally:
        if conn: conn.close()

def load_akc_logins(user_id: int) -> Optional[str]:
    """Загружает сохраненные логины пользователя."""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT logins FROM akc_login_defaults WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
        return result[0] if result else None
    finally:
        if conn: conn.close()


# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def create_excel_report(cert_data_list: List[Dict[str, Any]], user_threshold: int) -> io.BytesIO:
    """Создает Excel-отчет на основе данных сертификатов."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Отчет по сертификатам"
    ws.append(list(EXCEL_HEADERS))
    
    sorted_cert_data = sorted(cert_data_list, key=lambda x: x["Действителен до"])
    
    for cert_data in sorted_cert_data:
        row = [
            cert_data["ФИО"], 
            cert_data["Учреждение"], 
            cert_data["Серийный номер"], 
            cert_data["Действителен с"].strftime("%d.%m.%Y"), 
            cert_data["Действителен до"].strftime("%d.%m.%Y"), 
            cert_data["Осталось дней"]
        ]
        ws.append(row)
        
        last_row = ws.max_row
        days_left = cert_data["Осталось дней"]
        fill_color = None
        
        if days_left < 0:
            fill_color = RED_FILL
        elif 0 <= days_left <= user_threshold:
            fill_color = ORANGE_FILL
        else:
            fill_color = GREEN_FILL
            
        if fill_color:
            for cell in ws[last_row]:
                cell.fill = fill_color
                
    for column in ws.columns:
        max_length = 0
        column_letter = get_column_letter(column[0].column)
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2)
        ws.column_dimensions[column_letter].width = adjusted_width
        
    excel_buffer = io.BytesIO()
    wb.save(excel_buffer)
    excel_buffer.seek(0)
    return excel_buffer

def generate_summary_message(cert_data_list: List[Dict[str, Any]], user_threshold: int) -> str:
    """Генерирует сводное сообщение о скоро истекающих сертификатах."""
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
    """Извлекает информацию из файла сертификата."""
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
        
        return {
            "ФИО": subject_common_name,
            "Учреждение": organization_name,
            "Серийный номер": serial_number,
            "Действителен с": valid_from,
            "Действителен до": valid_until,
            "Осталось дней": days_left
        }
    except Exception as e:
        logger.error(f"Ошибка при парсинге сертификата: {e}")
        return None

def _process_file_content(file_bytes: bytes, file_name: str) -> List[Dict[str, Any]]:
    """Обрабатывает содержимое файла (сертификат или ZIP-архив)."""
    all_certs_data = []
    if file_name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), 'r') as z:
                for member in z.namelist():
                    if member.lower().endswith(ALLOWED_EXTENSIONS):
                        with z.open(member) as cert_file:
                            cert_info = get_certificate_info(cert_file.read())
                            if cert_info:
                                all_certs_data.append(cert_info)
        except zipfile.BadZipFile:
            logger.error(f"Получен поврежденный ZIP-файл: {file_name}", exc_info=True)
            return []
    elif file_name.lower().endswith(ALLOWED_EXTENSIONS):
        cert_info = get_certificate_info(file_bytes)
        if cert_info:
            all_certs_data.append(cert_info)
    return all_certs_data

def create_akc_docx(form_data: dict) -> io.BytesIO:
    """Создает DOCX-файл заявки АЦК с несколькими записями."""
    doc = docx.Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    new_width, new_height = section.page_height, section.page_width
    section.page_width = new_width
    section.page_height = new_height
    
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.5)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run("Приложение 5 к Регламенту взаимодействия\nминистерства финансов Амурской области и\nУчастников юридически значимого\nэлектронного документооборота")
    font = run.font
    font.name = 'Times New Roman'
    font.size = Pt(12)

    doc.add_paragraph() 

    p = doc.add_paragraph()
    run = p.add_run("От кого: ")
    run.bold = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)
    run = p.add_run(f"{form_data.get('sender_fio', '')}\n")
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)
    
    run = p.add_run("(Ф.И.О. представителя учреждения)\n")
    run.italic = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(10)
    
    run = p.add_run(f"{form_data.get('org_name', '')}\n")
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)

    run = p.add_run("(наименование учреждения)\n")
    run.italic = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(10)

    run = p.add_run(f"{form_data.get('inn_kpp', '')}\n")
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)

    run = p.add_run("(ИНН/КПП)\n")
    run.italic = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(10)

    run = p.add_run(f"{form_data.get('municipality', '')}\n")
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)

    run = p.add_run("(наименование муниципального образования)\n")
    run.italic = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(10)

    run = p.add_run(f"{datetime.now().strftime('%d.%m.%Y')}\n")
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)

    run = p.add_run("(дата)")
    run.italic = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(10)

    doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("ЗАЯВКА\nна регистрацию пользователя ЦИТП")
    run.bold = True
    run.font.name = 'Times New Roman'
    run.font.size = Pt(12)
    
    doc.add_paragraph()

    table = doc.add_table(rows=1, cols=7)
    table.style = 'Table Grid'
    
    headers = [
        "Субъект ЭП", "Роль субъекта в ЦИТП (Руководитель, Бухгалтер, Специалист ГИС ГМП)", 
        "Наименование ЦИТП (АЦК-Финансы, АЦК-Планирование)", 
        "Серийный номер сертификата", "Имя файла сертификата", 
        "Имя пользователя для входа в ЦИТП, под которым производится подписание", 
        "Действие(добавить, удалить, заменить, заблокировать)"
    ]
    
    for i, cell in enumerate(table.rows[0].cells):
        cell.text = headers[i]
        cell.paragraphs[0].runs[0].font.bold = True
        cell.paragraphs[0].runs[0].font.size = Pt(10)

    # Добавляем строки для каждого сертификата
    for cert_data in form_data.get('certificates', []):
        row_cells = table.add_row().cells
        row_cells[0].text = cert_data.get('cert_owner', '')
        row_cells[1].text = cert_data.get('role', '')
        row_cells[2].text = cert_data.get('citp_name', '')
        row_cells[3].text = cert_data.get('cert_serial', '')
        row_cells[4].text = cert_data.get('cert_filename', '')
        row_cells[5].text = cert_data.get('logins', '')
        row_cells[6].text = cert_data.get('action', '')
        for cell in row_cells:
            cell.paragraphs[0].runs[0].font.size = Pt(10)

    doc.add_paragraph()

    footer_table = doc.add_table(rows=4, cols=3)
    
    footer_table.cell(0, 0).text = "№ Записи в электронном журнале:"
    footer_table.cell(0, 1).text = "Статус:  выполнено/отказано"
    
    footer_table.cell(1, 0).text = "Заведение пользователя:"
    footer_table.cell(1, 1).text = "Подпись представителя учреждения"
    footer_table.cell(1, 2).text = "Дата"
    
    footer_table.cell(2, 0).text = "Дата:"
    footer_table.cell(2, 1).text = "Исполнитель:"
    
    footer_table.cell(3, 0).text = "Установка СКП ЭП"
    footer_table.cell(3, 1).text = "М.П."
    footer_table.cell(3, 2).text = "Номер телефона или e-mail"

    for row in footer_table.rows:
        for cell in row.cells:
            if cell.text:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.name = 'Times New Roman'
                        run.font.size = Pt(12)

    doc_buffer = io.BytesIO()
    doc.save(doc_buffer)
    doc_buffer.seek(0)
    return doc_buffer


# --- 5. ОБРАБОТЧИКИ КОМАНД, КНОПОК И ДИАЛОГОВ ---
async def get_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет пользователю его Telegram ID."""
    user_id = update.effective_user.id
    await update.message.reply_text(f"Ваш User ID: `{user_id}`", parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start. Отображает главное меню в зависимости от прав."""
    user = update.effective_user
    user_id = user.id
    
    # Динамическое формирование клавиатуры на основе разрешений
    keyboard = []
    row1 = []
    if has_permission(user_id, "cert_analysis"):
        row1.append("📜 Анализ сертификатов")
    if has_permission(user_id, "settings"):
        row1.append("⚙️ Настройки анализа сертификатов")
    if row1:
        keyboard.append(row1)

    row2 = []
    if has_permission(user_id, "youtube"):
        row2.append("🎬 Скачивание с YouTube")
    if has_permission(user_id, "akc_form"):
        row2.append("📄 Заявка АЦК")
    if row2:
        keyboard.append(row2)
        
    keyboard.append(["❓ Помощь"]) # Помощь доступна всем авторизованным пользователям

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    start_message = (
        f"Привет, {user.mention_html()}! 👋\n\n"
        "Я — ваш многофункциональный помощник. Выберите доступное действие на клавиатуре."
    )
    await update.message.reply_html(start_message, reply_markup=reply_markup)
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки 'Помощь'. Отправляет справочную информацию."""
    help_text = (
        "Я могу помочь вам с несколькими задачами:\n\n"
        "📜 **Анализ сертификатов**\n"
        "Нажмите кнопку и отправьте файлы `.cer`, `.crt` или `.zip`-архив для создания Excel-отчета.\n\n"
        "📄 **Заявка АЦК**\n"
        "Нажмите кнопку, чтобы запустить пошаговый мастер создания заявки в формате DOCX.\n\n"
        "🎬 **Скачивание с YouTube**\n"
        "Нажмите кнопку и отправьте ссылку, чтобы скачать видео.\n\n"
        "⚙️ **Настройки анализа сертификатов**\n"
        "Измените порог оповещения об истекающих сертификатах."
    )
    await update.message.reply_text(help_text)

async def request_certificate_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пользователю подробное описание функции анализа сертификатов
    и запрашивает файлы для анализа.
    """
    description_text = (
        "**Анализ цифровых сертификатов** 📊\n\n"
        "Эта функция предназначена для проверки сроков действия цифровых сертификатов.\n\n"
        "**Как это работает:**\n"
        "1. Вы отправляете мне файлы сертификатов (`.cer`, `.crt`, `.pem`, `.der`) по одному или в виде ZIP-архива.\n"
        "2. Я извлекаю из них ключевую информацию: ФИО владельца, организацию, серийный номер и срок действия.\n"
        "3. Я формирую и отправляю вам два результата:\n"
        "   - **Краткое сообщение** со списком сертификатов, которые скоро истекают.\n"
        "   - **Подробный Excel-отчет** со всеми данными, где строки подсвечены цветом в зависимости от оставшегося срока действия (красный - просрочен, оранжевый - скоро истекает, зеленый - действителен).\n\n"
        f"Пожалуйста, отправьте мне файл(ы) ({', '.join(ALLOWED_EXTENSIONS)}) или ZIP-архив для анализа."
    )
    await update.message.reply_text(description_text, parse_mode='Markdown')

async def handle_simple_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на простые кнопки главного меню."""
    button_text = update.message.text
    if button_text == "❓ Помощь":
        await help_command(update, context)
    elif button_text == "📜 Анализ сертификатов":
        await request_certificate_files(update, context)

def download_video_sync(url: str, ydl_opts: dict) -> str:
    """Синхронная функция для скачивания видео с помощью yt-dlp."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

async def handle_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает полученную ссылку на YouTube видео."""
    url = update.message.text
    msg = await update.message.reply_text("Получаю информацию о видео...")
    
    try:
        ydl_opts_info = {'quiet': True, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info_dict = ydl.extract_info(url, download=False)
        
        filesize = info_dict.get('filesize') or info_dict.get('filesize_approx')
        title = info_dict.get('title', 'Без названия')
        
        if not filesize:
            await msg.edit_text("❌ Не удалось определить размер видео."); return ConversationHandler.END

        if filesize > MAX_VIDEO_SIZE_BYTES:
            size_in_mb = filesize / 1024 / 1024
            await msg.edit_text(f"❌ Видео '{title}' слишком большое ({size_in_mb:.1f} МБ)."); return ConversationHandler.END

        context.user_data['youtube_url'] = url
        context.user_data['youtube_title'] = title
        
        size_in_mb = filesize / 1024 / 1024
        keyboard = [[InlineKeyboardButton("✅ Да, скачать", callback_data='yt_confirm'), InlineKeyboardButton("❌ Нет, отмена", callback_data='yt_cancel')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg.edit_text(f"**Название:** {title}\n**Размер:** {size_in_mb:.1f} МБ\n\nНачать скачивание?", reply_markup=reply_markup, parse_mode='Markdown')
        return CONFIRMING_DOWNLOAD

    except Exception as e:
        logger.error(f"Ошибка при получении информации о YouTube видео: {e}", exc_info=True)
        await msg.edit_text(f"❌ Не удалось получить информацию по ссылке: {url}"); return ConversationHandler.END

async def start_download_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает скачивание видео после подтверждения."""
    query = update.callback_query
    await query.answer()
    url = context.user_data.get('youtube_url')
    title = context.user_data.get('youtube_title', 'видео')
    user_id = update.effective_user.id

    if not url:
        await query.edit_message_text("❌ Произошла ошибка, начните заново."); return ConversationHandler.END

    await query.edit_message_text(f"Начинаю загрузку '{title}'...")
    
    ydl_opts = {'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', 'outtmpl': f'{uuid.uuid4()}.%(ext)s', 'quiet': True}
    
    try:
        video_filename = await asyncio.to_thread(download_video_sync, url, ydl_opts)
        await query.edit_message_text("Видео скачано. Отправляю...")
        with open(video_filename, 'rb') as video_file:
            await context.bot.send_video(chat_id=user_id, video=video_file, supports_streaming=True, read_timeout=120, write_timeout=120)
        os.remove(video_filename)
        await query.message.delete()
    except Exception as e:
        logger.error(f"Ошибка при скачивании/отправке видео: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Не удалось обработать видео: {url}")
    
    return ConversationHandler.END

async def cancel_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет скачивание видео."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Скачивание отменено.")
    return ConversationHandler.END

async def youtube_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа в диалог скачивания с YouTube."""
    await update.message.reply_text("Пожалуйста, отправьте ссылку на YouTube видео.")
    return AWAITING_YOUTUBE_LINK

async def invalid_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает неверную ссылку на YouTube."""
    await update.message.reply_text("Это не похоже на ссылку YouTube. Пожалуйста, отправьте правильную ссылку или отмените действие.")
    return AWAITING_YOUTUBE_LINK

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает полученный документ (сертификат или архив)."""
    document = update.message.document
    if document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ Файл слишком большой. Максимум: {MAX_FILE_SIZE / 1024 / 1024:.0f} МБ.")
        return
        
    user_id = update.effective_user.id
    user_threshold = await get_user_threshold(user_id, context)
    file_name = document.file_name
    logger.info(f"Получен файл: {file_name} от {user_id}")
    await update.message.reply_text("Анализирую...")
    
    try:
        file_object = await context.bot.get_file(document.file_id)
        file_buffer = io.BytesIO()
        await file_object.download_to_memory(file_buffer)
        file_buffer.seek(0)
        
        all_certs_data = _process_file_content(file_buffer.read(), file_name)
        
        if not all_certs_data:
            await update.message.reply_text("Не удалось найти/проанализировать сертификаты.")
            return
            
        excel_buffer = create_excel_report(all_certs_data, user_threshold)
        summary_message = generate_summary_message(all_certs_data, user_threshold)
        
        await update.message.reply_text(summary_message)
        await update.message.reply_document(document=excel_buffer, filename="Сертификаты_отчет.xlsx")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке документа: {e}", exc_info=True)
        await update.message.reply_text(f"Произошла непредвиденная ошибка.")

async def handle_wrong_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает файл с неверным расширением."""
    await update.message.reply_text(f"❌ Неверный формат файла. Нужны: {', '.join(ALLOWED_EXTENSIONS)}, .zip")

async def settings_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа в диалог настроек."""
    user_id = update.effective_user.id
    current_threshold = await get_user_threshold(user_id, context)
    keyboard = [[InlineKeyboardButton("Изменить порог", callback_data='change_threshold')], [InlineKeyboardButton("Назад", callback_data='back_to_main')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"⚙️ **Настройки**\nТекущий порог: **{current_threshold}** дней.", reply_markup=reply_markup, parse_mode='Markdown')
    return CHOOSING_ACTION

async def prompt_for_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает у пользователя новое значение порога."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="Отправьте новое число дней (например, 60).")
    return TYPING_DAYS

async def set_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Устанавливает новый порог дней."""
    user_id = update.effective_user.id
    try:
        new_threshold = int(update.message.text)
        if new_threshold <= 0:
            await update.message.reply_text("❌ Введите положительное число.")
            return TYPING_DAYS
            
        context.user_data['threshold'] = new_threshold
        save_user_threshold(user_id, new_threshold)
        await update.message.reply_html(f"✅ Порог изменен и сохранен: <b>{new_threshold}</b> дней.")
        
    except (ValueError):
        await update.message.reply_text("❌ Это не число. Отправьте, например: 60")
        return TYPING_DAYS
        
    return ConversationHandler.END

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершает диалог настроек."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="Настройки закрыты.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий диалог."""
    await update.message.reply_text('Действие отменено.', reply_markup=ReplyKeyboardRemove())
    # Очищаем данные формы, чтобы избежать проблем при следующем запуске
    context.user_data.pop('akc_form', None)
    return ConversationHandler.END

# --- ЛОГИКА ДИАЛОГА ЗАЯВКИ АЦК ---

async def akc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог создания заявки АЦК и выводит описание."""
    user_id = update.effective_user.id
    context.user_data['akc_form'] = {
        'certificates': [] # Инициализируем список для сертификатов
    }

    description = (
        "**Мастер создания заявки АЦК** 📄\n\n"
        "Этот мастер поможет вам пошагово сформировать заявку на регистрацию пользователя ЦИТП.\n\n"
        "**Что он делает:**\n"
        "1. Запрашивает данные для шапки документа (ФИО, организация и т.д.).\n"
        "2. Сохраняет эти данные как шаблон для ускорения работы в будущем.\n"
        "3. Просит прикрепить **один или несколько** файлов сертификатов.\n"
        "4. Помогает поочередно настроить каждую запись в заявке.\n"
        "5. В итоге создает **ZIP-архив**, содержащий готовую заявку в формате **DOCX** и все прикрепленные сертификаты.\n\n"
        "------------------------------------\n\n"
    )

    defaults = load_akc_defaults(user_id)
    if defaults:
        context.user_data['akc_form'].update(defaults)
        text = description + (
            "Найдены сохраненные данные для шапки заявки:\n\n"
            f"• **От кого:** {defaults['sender_fio']}\n"
            f"• **Учреждение:** {defaults['org_name']}\n"
            f"• **ИНН/КПП:** {defaults['inn_kpp']}\n"
            f"• **МО:** {defaults['municipality']}\n\n"
            "Использовать эти данные?"
        )
        keyboard = [[InlineKeyboardButton("✅ Да, использовать", callback_data='akc_use_defaults')], [InlineKeyboardButton("✏️ Заполнить заново", callback_data='akc_refill')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        return AKC_CONFIRM_DEFAULTS
    else:
        text = description + "Начинаем! Введите **ФИО представителя учреждения**:"
        await update.message.reply_text(text, parse_mode='Markdown')
        return AKC_SENDER_FIO

async def akc_use_defaults(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Использует сохраненный шаблон для шапки и запрашивает файлы."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Данные шапки применены.")
    
    keyboard = ReplyKeyboardMarkup([["Готово"]], resize_keyboard=True, one_time_keyboard=True)
    await query.message.reply_text(
        "Теперь отправьте мне **один или несколько** файлов сертификатов (.cer, .crt).\n"
        "Когда закончите, нажмите кнопку **'Готово'**.",
        reply_markup=keyboard
    )
    return AKC_AWAIT_CERTIFICATES

async def akc_refill_defaults(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает процесс повторного заполнения шапки заявки."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Хорошо, давайте заполним данные заново.\n\nВведите **ФИО представителя учреждения**:", parse_mode='Markdown')
    return AKC_SENDER_FIO

async def akc_get_sender_fio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['akc_form']['sender_fio'] = update.message.text
    await update.message.reply_text("Введите **полное наименование учреждения**:", parse_mode='Markdown')
    return AKC_ORG_NAME

async def akc_get_org_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['akc_form']['org_name'] = update.message.text
    await update.message.reply_text("Введите **ИНН/КПП** учреждения:", parse_mode='Markdown')
    return AKC_INN_KPP

async def akc_get_inn_kpp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['akc_form']['inn_kpp'] = update.message.text
    await update.message.reply_text("Введите **наименование муниципального образования**:", parse_mode='Markdown')
    return AKC_MUNICIPALITY

async def akc_get_municipality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает МО, сохраняет шаблон и запрашивает файлы."""
    user_id = update.effective_user.id
    context.user_data['akc_form']['municipality'] = update.message.text
    save_akc_defaults(user_id, context.user_data['akc_form'])
    await update.message.reply_text("Шапка заявки заполнена и сохранена.")
    
    keyboard = ReplyKeyboardMarkup([["Готово"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "Теперь отправьте мне **один или несколько** файлов сертификатов (.cer, .crt).\n"
        "Когда закончите, нажмите кнопку **'Готово'**.",
        reply_markup=keyboard
    )
    return AKC_AWAIT_CERTIFICATES

async def akc_add_certificate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает и добавляет в список полученный файл сертификата."""
    document = update.message.document
    file_id = document.file_id
    
    try:
        file_object = await context.bot.get_file(file_id)
        file_buffer = io.BytesIO()
        await file_object.download_to_memory(file_buffer)
        cert_bytes = file_buffer.getvalue()
        
        cert_data = get_certificate_info(cert_bytes)
        
        if not cert_data:
            await update.message.reply_text(f"❌ Не удалось прочитать данные из файла `{document.file_name}`. Попробуйте другой файл.", parse_mode='Markdown')
            return AKC_AWAIT_CERTIFICATES

        context.user_data['akc_form']['certificates'].append({
            'cert_owner': cert_data['ФИО'],
            'cert_serial': cert_data['Серийный номер'],
            'cert_filename': document.file_name,
            'cert_bytes': cert_bytes,
            'role': '', 'citp_name': '', 'logins': '', 'action': ''
        })
        
        await update.message.reply_text(f"✅ Сертификат `{document.file_name}` добавлен. Отправьте следующий или нажмите 'Готово'.", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Ошибка при обработке файла сертификата для заявки: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке файла.")
        
    return AKC_AWAIT_CERTIFICATES

async def akc_start_data_loop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает цикл настройки для каждого добавленного сертификата."""
    if not context.user_data['akc_form'].get('certificates'):
        await update.message.reply_text("Вы не добавили ни одного сертификата. Действие отменено.", reply_markup=ReplyKeyboardRemove())
        context.user_data.pop('akc_form', None)
        return ConversationHandler.END

    await update.message.reply_text("Отлично! Все файлы приняты. Начинаем настройку каждой записи.", reply_markup=ReplyKeyboardRemove())
    context.user_data['akc_form']['cert_index'] = 0
    await _akc_ask_for_role(update, context)
    return AKC_ROLE

async def _akc_ask_for_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вспомогательная функция для запроса роли для текущего сертификата."""
    cert_index = context.user_data['akc_form']['cert_index']
    cert_list = context.user_data['akc_form']['certificates']
    cert_data = cert_list[cert_index]
    cert_owner = cert_data['cert_owner']
    
    text = (
        f"➡️ **Настройка записи {cert_index + 1} из {len(cert_list)}**\n"
        f"Владелец: **{cert_owner}**\n\n"
        "Выберите **роль субъекта**:"
    )
    keyboard = [
        [InlineKeyboardButton("Руководитель", callback_data='role_Руководитель')],
        [InlineKeyboardButton("Бухгалтер", callback_data='role_Бухгалтер')],
        [InlineKeyboardButton("Специалист ГИС ГМП", callback_data='role_Специалист ГИС ГМП')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def akc_get_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает роль для текущего сертификата."""
    query = update.callback_query
    await query.answer()
    role = query.data.split('_')[1]
    
    cert_index = context.user_data['akc_form']['cert_index']
    context.user_data['akc_form']['certificates'][cert_index]['role'] = role

    keyboard = [[InlineKeyboardButton("АЦК-Финансы", callback_data='citp_АЦК-Финансы')], [InlineKeyboardButton("АЦК-Планирование", callback_data='citp_АЦК-Планирование')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=f"Выбрана роль: {role}.\n\nВыберите **Наименование ЦИТП**:", reply_markup=reply_markup, parse_mode='Markdown')
    return AKC_CITP_NAME

async def akc_get_citp_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает ЦИТП и проверяет сохраненные логины."""
    query = update.callback_query
    await query.answer()
    citp_name = query.data.split('_')[1]

    cert_index = context.user_data['akc_form']['cert_index']
    context.user_data['akc_form']['certificates'][cert_index]['citp_name'] = citp_name
    
    user_id = update.effective_user.id
    saved_logins = load_akc_logins(user_id)
    
    if saved_logins:
        context.user_data['akc_saved_logins'] = saved_logins
        text = (
            f"Выбрана система: {citp_name}.\n\n"
            f"Найдены ранее сохраненные логины: `{saved_logins}`\n\n"
            "Использовать их?"
        )
        keyboard = [
            [InlineKeyboardButton("✅ Да, использовать", callback_data='logins_use_saved')],
            [InlineKeyboardButton("✏️ Ввести новые", callback_data='logins_enter_new')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='Markdown')
        return AKC_CONFIRM_LOGINS
    else:
        await query.edit_message_text(text=f"Выбрана система: {citp_name}.\n\nВведите **имена пользователей (логины)**, через запятую:", parse_mode='Markdown')
        return AKC_LOGINS

async def akc_use_saved_logins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Использует сохраненные логины."""
    query = update.callback_query
    await query.answer()
    
    saved_logins = context.user_data.get('akc_saved_logins')
    cert_index = context.user_data['akc_form']['cert_index']
    context.user_data['akc_form']['certificates'][cert_index]['logins'] = saved_logins
    
    keyboard = [[InlineKeyboardButton(text, callback_data=f'action_{text}') for text in ["Добавить", "Удалить"]], [InlineKeyboardButton(text, callback_data=f'action_{text}') for text in ["Заменить", "Заблокировать"]]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Логины применены.\n\nВыберите **действие** с сертификатом:", reply_markup=reply_markup, parse_mode='Markdown')
    
    context.user_data.pop('akc_saved_logins', None)
    return AKC_ACTION

async def akc_enter_new_logins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает ввод новых логинов."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите **новые имена пользователей (логины)**, через запятую:", parse_mode='Markdown')
    return AKC_LOGINS

async def akc_get_logins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает и сохраняет новые логины."""
    user_id = update.effective_user.id
    logins = update.message.text
    
    cert_index = context.user_data['akc_form']['cert_index']
    context.user_data['akc_form']['certificates'][cert_index]['logins'] = logins
    save_akc_logins(user_id, logins)
    
    keyboard = [[InlineKeyboardButton(text, callback_data=f'action_{text}') for text in ["Добавить", "Удалить"]], [InlineKeyboardButton(text, callback_data=f'action_{text}') for text in ["Заменить", "Заблокировать"]]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Новые логины сохранены.\n\nВыберите **действие** с сертификатом:", reply_markup=reply_markup, parse_mode='Markdown')
    return AKC_ACTION

async def akc_get_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает действие и решает, продолжать цикл или завершать."""
    query = update.callback_query
    await query.answer()
    action = query.data.split('_')[1]

    cert_index = context.user_data['akc_form']['cert_index']
    context.user_data['akc_form']['certificates'][cert_index]['action'] = action

    cert_index += 1
    context.user_data['akc_form']['cert_index'] = cert_index

    if cert_index < len(context.user_data['akc_form']['certificates']):
        await _akc_ask_for_role(update, context)
        return AKC_ROLE
    else:
        await query.edit_message_text("Все записи настроены. Формирую итоговый ZIP-архив...")
        await akc_finish(update, context)
        context.user_data.pop('akc_form', None)
        return ConversationHandler.END

async def akc_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завершает создание заявки, формирует и отправляет ZIP-архив."""
    form_data = context.user_data['akc_form']
    try:
        docx_buffer = create_akc_docx(form_data)
        docx_filename = f"Заявка_АЦК_{form_data.get('sender_fio', 'пользователь')}.docx"
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
            zip_file.writestr(docx_filename, docx_buffer.getvalue())
            for cert_data in form_data.get('certificates', []):
                if cert_data.get('cert_bytes') and cert_data.get('cert_filename'):
                    zip_file.writestr(cert_data['cert_filename'], cert_data['cert_bytes'])
        zip_buffer.seek(0)
        
        zip_filename = f"Заявка_АЦК_{form_data.get('sender_fio', 'пользователь')}.zip"
        
        # Отправляем документ от имени сообщения, а не коллбэка, чтобы не было ошибок
        await context.bot.send_document(chat_id=update.effective_chat.id, document=zip_buffer, filename=zip_filename, caption="✅ Ваша заявка и сертификат в ZIP-архиве.")
        
    except Exception as e:
        logger.error(f"Ошибка при создании или отправке ZIP-архива: {e}", exc_info=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Произошла ошибка при создании архива.")


# --- 6. ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
async def main() -> None:
    """Главная функция для настройки и запуска бота."""
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        logger.error("Не найден токен или URL базы данных. Проверьте переменные окружения.")
        return
        
    init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    cancel_handler = MessageHandler(filters.Regex('^/cancel$') | filters.Regex('^Отмена$'), cancel)
    
    settings_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^⚙️ Настройки анализа сертификатов$') & PermissionFilter("settings"), settings_entry)],
        states={
            CHOOSING_ACTION: [CallbackQueryHandler(prompt_for_days, pattern='^change_threshold$'), CallbackQueryHandler(end_conversation, pattern='^back_to_main$')],
            TYPING_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_days)],
        },
        fallbacks=[CommandHandler('start', start), cancel_handler],
    )
    
    youtube_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🎬 Скачивание с YouTube$') & PermissionFilter("youtube"), youtube_entry)],
        states={
            AWAITING_YOUTUBE_LINK: [MessageHandler(filters.Regex(YOUTUBE_URL_PATTERN), handle_youtube_link)],
            CONFIRMING_DOWNLOAD: [CallbackQueryHandler(start_download_confirmed, pattern='^yt_confirm$'), CallbackQueryHandler(cancel_download, pattern='^yt_cancel$')]
        },
        fallbacks=[CommandHandler('start', start), cancel_handler]
    )
    
    akc_cert_filter = filters.Document.FileExtension("cer") | filters.Document.FileExtension("crt")
    
    akc_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📄 Заявка АЦК$') & PermissionFilter("akc_form"), akc_start)],
        states={
            AKC_CONFIRM_DEFAULTS: [CallbackQueryHandler(akc_use_defaults, pattern='^akc_use_defaults$'), CallbackQueryHandler(akc_refill_defaults, pattern='^akc_refill$')],
            AKC_SENDER_FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, akc_get_sender_fio)],
            AKC_ORG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, akc_get_org_name)],
            AKC_INN_KPP: [MessageHandler(filters.TEXT & ~filters.COMMAND, akc_get_inn_kpp)],
            AKC_MUNICIPALITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, akc_get_municipality)],
            AKC_AWAIT_CERTIFICATES: [
                MessageHandler(akc_cert_filter, akc_add_certificate),
                MessageHandler(filters.Regex('^Готово$'), akc_start_data_loop)
            ],
            AKC_ROLE: [CallbackQueryHandler(akc_get_role, pattern='^role_')],
            AKC_CITP_NAME: [CallbackQueryHandler(akc_get_citp_name, pattern='^citp_')],
            AKC_CONFIRM_LOGINS: [
                CallbackQueryHandler(akc_use_saved_logins, pattern='^logins_use_saved$'),
                CallbackQueryHandler(akc_enter_new_logins, pattern='^logins_enter_new$')
            ],
            AKC_LOGINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, akc_get_logins)],
            AKC_ACTION: [CallbackQueryHandler(akc_get_action, pattern='^action_')],
        },
        fallbacks=[CommandHandler('start', start), cancel_handler],
        per_message=False
    )
    
    application.add_handler(settings_conv_handler)
    application.add_handler(youtube_conv_handler)
    application.add_handler(akc_conv_handler)
    
    application.add_handler(CommandHandler("my_id", get_my_id))
    application.add_handler(CommandHandler("start", start, filters=authorized_user_filter))
    application.add_handler(CommandHandler("cert", request_certificate_files, filters=PermissionFilter("cert_analysis")))
    
    application.add_handler(MessageHandler(filters.Regex("^(📜 Анализ сертификатов)$") & PermissionFilter("cert_analysis"), handle_simple_buttons))
    application.add_handler(MessageHandler(filters.Regex("^(❓ Помощь)$") & authorized_user_filter, handle_simple_buttons))
    
    allowed_docs_filter = (
        filters.Document.FileExtension("zip") | filters.Document.FileExtension("cer") |
        filters.Document.FileExtension("crt") | filters.Document.FileExtension("pem") |
        filters.Document.FileExtension("der")
    )
    application.add_handler(MessageHandler(allowed_docs_filter & ~filters.COMMAND & PermissionFilter("cert_analysis"), handle_document))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & authorized_user_filter, handle_wrong_document))

    logger.info("Запускаю бота...")
    async with application:
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await asyncio.Future()


# --- 7. ТОЧКА ВХОДА ---
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
