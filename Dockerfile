FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_DEFAULT_DATA_DIR=/data \
    APP_ALLOWED_ORIGINS=https://vivaca86.github.io

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app_server.py index.html ./
COPY assets ./assets
COPY examples ./examples
COPY game_data_engine ./game_data_engine

RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "app_server.py"]
