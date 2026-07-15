# ── Stage 1: сборка React-фронта ────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /web
COPY web/package.json ./
RUN npm install
COPY web/ ./
RUN npm run build

# ── Stage 2: бот + веб-API в одном образе/контейнере ────────────────
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all bot modules from the local 'app' directory into the container
COPY app/*.py ./

# Собранная статика фронта — FastAPI (web_api.py) отдаёт её сам,
# отдельный nginx-контейнер не нужен (см. static/ mount в web_api.py)
COPY --from=frontend /web/dist ./static

# Create directory for persistent data storage
RUN mkdir -p /app/data

# Run the bot with unbuffered logging
CMD ["python", "-u", "main.py"]
