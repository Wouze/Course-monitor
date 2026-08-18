FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    USERS_FILE=/app/data/users.json \
    SESSION_FILE=/app/data/session.json

RUN mkdir -p /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py edugate.py ./

CMD ["python", "bot.py"]
