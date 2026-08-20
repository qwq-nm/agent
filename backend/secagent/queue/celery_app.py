from celery import Celery

from secagent.config import get_settings

settings = get_settings()
celery = Celery("secagent", broker=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_ignore_result=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.worker_concurrency,
    broker_connection_retry_on_startup=True,
)
