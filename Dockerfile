FROM node:20-alpine AS frontend
WORKDIR /web
# Lockfile + `npm ci`: reproducible frontend builds. Without it every CI run resolved the
# newest Rollup/Vite, so a bad upstream release broke the image with no change in this repo.
# To upgrade dependencies deliberately: `cd web && npm update`, commit package-lock.json.
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all bot modules from the local 'app' directory into the container
COPY app/*.py ./

# App version (shown in the dashboard) + the exact commit this image was built from
COPY VERSION ./VERSION
ARG GIT_SHA=
ENV MUFCA_COMMIT=$GIT_SHA


COPY --from=frontend /web/dist ./static

# Create directory for persistent data storage
RUN mkdir -p /app/data

# Run the bot with unbuffered logging
CMD ["python", "-u", "main.py"]
