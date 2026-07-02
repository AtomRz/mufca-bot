FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all bot modules from the local 'app' directory into the container
COPY app/*.py ./

# Create directory for persistent data storage
RUN mkdir -p /app/data

# Run the bot with unbuffered logging
CMD ["python", "-u", "main.py"]
