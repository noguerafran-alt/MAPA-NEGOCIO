FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render inyecta PORT en runtime; 10000 es el default si se corre suelto (docker run).
ENV PORT=10000
EXPOSE 10000

CMD gunicorn app:app --workers 1 --threads 2 --timeout 300 --bind 0.0.0.0:${PORT}
