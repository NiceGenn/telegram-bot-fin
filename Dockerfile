# Используем официальный образ Python
FROM python:3.10-slim

# Устанавливаем системные зависимости, включая ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем основной код бота
COPY bot.py .

# Команда, которая будет выполняться при запуске контейнера
CMD ["python3", "bot.py"]