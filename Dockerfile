FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p /data && groupadd --gid 10001 bot && useradd --create-home --uid 10001 --gid 10001 bot && chown bot:bot /data
USER bot
CMD ["python", "-m", "app.bot"]
