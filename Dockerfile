# PE engine (Python FastAPI). Railway injects PORT; uvicorn binds to it.
FROM python:3.12-slim
WORKDIR /app

# System deps some wheels may need (pandas/openpyxl are pure-wheel, but keep gcc for safety)
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

COPY . .
RUN pip install --no-cache-dir .

ENV ANGELIC_ENV=production
ENV ANGELIC_OUTPUT_DIR=/app/outputs
# Bind to Railway's assigned port (falls back to 8011 locally).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8011}"]
