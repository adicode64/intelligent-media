#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting Celery worker in background..."
celery -A app.core.celery_app.celery_app worker \
    --loglevel=INFO \
    -Q image_processing \
    --concurrency=2 &

echo "Starting API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"