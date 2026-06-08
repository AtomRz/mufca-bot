FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота
COPY mufca_v3.py .

# Запуск
CMD ["python", "-u", "mufca_v3.py"]
