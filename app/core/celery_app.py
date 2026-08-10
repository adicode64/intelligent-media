"""
Celery application.

Queue strategy: a single dedicated `image_processing` queue. Image analysis is
CPU-bound and homogeneous, so splitting queues would add operational surface
without benefit at this scale. `acks_late` + `reject_on_worker_lost` means a task
killed mid-flight is redelivered instead of silently lost.
"""

from __future__ import annotations

from celery import Celery

from app.config import settings
from app.core.logging import configure_logging

configure_logging()

celery_app = Celery(
    "media_pipeline",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=["app.workers.image_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_default_queue="image_processing",
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,  # guards against native-lib memory creep
    task_time_limit=300,
    task_soft_time_limit=240,
    result_expires=3600,
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=False,
)
