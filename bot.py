# =================================================================================
#    ФИНАЛЬНАЯ ВЕРСИЯ БОТА (V31 - С ПРОВЕРКОЙ СТАТУСА ОТОЗВАННОСТИ OCSP)
# =================================================================================

# --- 1. ИМПОРТЫ ---
# ... (все импорты остаются прежними)
from ocspchecker import ocspchecker # <<< НОВОЕ: Импорт для OCSP
# ...

# --- 2. НАСТРОЙКА И КОНСТАНТЫ ---
# ... (все константы без изменений, кроме заголовков Excel)
EXCEL_HEADERS: Tuple[str, ...] = ("Статус", "ФИО", "Учреждение", "Серийный номер", "Действителен с", "Действителен до", "Осталось дней")
# ...

# --- 3. ВЕБ-СЕРВЕР FASTAPI (без изменений) ---

# --- 4. РАБОТА С БАЗОЙ ДАННЫХ POSTGRESQL (без изменений) ---

# --- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ОБРАБОТЧИКИ ---

# <<< НОВОЕ: Функция для проверки статуса отзыва >>>
def check_ocsp_status(cert: x509.Certificate) -> str:
    """Проверяет статус сертификата через OCSP."""
    try:
        # Пытаемся извлечь URL OCSP-ответчика из сертификата
        ocsp_url_ext = cert.extensions.get_extension_for_oid(x509.OID_AUTHORITY_INFORMATION_ACCESS)
        ocsp_descriptions = [desc for desc in ocsp_url_ext.value if desc.access_method == x509.OID_OCSP]
        if not ocsp_descriptions:
            return "Нет OCSP"

        ocsp_url = ocsp_descriptions[0].access_location.value
        
        # Выполняем проверку
        status = ocspchecker.get_ocsp_status(cert, ocsp_url=ocsp_url)
        
        if status == "OCSP Status: GOOD":
            return "Действителен"
        elif status == "OCSP Status: REVOKED":
            return "ОТОЗВАН"
        else:
            return "Неизвестно"
            
    except x509.ExtensionNotFound:
        return "Нет OCSP"
    except Exception as e:
        logger.error(f"Ошибка при проверке OCSP: {e}")
        return "Ошибка OCSP"

# <<< ИЗМЕНЕНИЕ: Добавлена проверка отзыва >>>
def get_certificate_info(cert_bytes: bytes) -> Optional[Dict[str, Any]]:
    try:
        try: cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())
        except ValueError: cert = x509.load_der_x509_certificate(cert_bytes, default_backend())
        
        # ... (извлечение ФИО, Учреждения и т.д. без изменений)
        
        # Получаем статус отзыва
        revocation_status = check_ocsp_status(cert)

        return {
            "Статус": revocation_status, # <-- Новое поле
            "ФИО": subject_common_name,
            # ... (остальные поля)
        }
    except Exception as e:
        logger.error(f"Ошибка при парсинге сертификата: {e}"); return None

# <<< ИЗМЕНЕНИЕ: Добавлена колонка "Статус" >>>
def create_excel_report(cert_data_list: List[Dict[str, Any]], user_threshold: int) -> io.BytesIO:
    # ... (код создания Workbook без изменений)
    ws.append(list(EXCEL_HEADERS)) # Добавляем заголовки, включая новый
    
    # ... (сортировка без изменений)
    
    for cert_data in sorted_cert_data:
        row = [
            cert_data["Статус"], # <-- Новая колонка
            cert_data["ФИО"],
            cert_data["Учреждение"],
            # ... (остальные поля)
        ]
        ws.append(row)
        
        days_left = cert_data["Осталось дней"]
        fill_color = None
        
        # <<< ИЗМЕНЕНИЕ: Отозванные всегда красим красным
        if cert_data["Статус"] == "ОТОЗВАН":
            fill_color = RED_FILL
        elif days_left < 0: 
            fill_color = RED_FILL
        elif 0 <= days_left <= user_threshold: 
            fill_color = ORANGE_FILL
        else: 
            fill_color = GREEN_FILL
            
        if fill_color:
            for cell in ws[last_row]: cell.fill = fill_color
            
    # ... (остальной код функции без изменений)
    return excel_buffer

# ... (остальные функции остаются без изменений)

# --- 6. ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА (без изменений) ---

# --- 7. ТОЧКА ВХОДА ДЛЯ ЗАПУСКА СКРИПТА (без изменений) ---
