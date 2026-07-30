FROM python:3.11-slim

WORKDIR /app

# Отключение кэширования bytecode Python и буферизация stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Копирование требований и установка
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY main.py .

# Папка автозагрузки торрентов
RUN mkdir -p /watch

CMD ["python", "main.py"]
