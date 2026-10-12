"""eljazari.ai — Celery application instance."""

from celery import Celery
from config.settings import settings

app = Celery(
    "eljazari",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["tasks.simulation"],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "tasks.simulation.*": {"queue": "simulation"},
    },
)
