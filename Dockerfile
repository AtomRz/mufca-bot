FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота (все модули)
COPY main.py .
COPY config.py .
COPY bot.py .
COPY signals.py .
COPY state.py .
COPY indicators.py .
COPY utils.py .
COPY volume_indicators.py .
COPY onchain.py .
COPY chart.py .

# Директория для данных
RUN mkdir -p /app/data

# Запуск
CMD ["python", "-u", "main.py"]
