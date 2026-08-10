FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# tesseract-ocr: OCR engine. libgl/libglib: OpenCV runtime deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user; uploaded content is never executed.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/storage \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
