"""Celery application instance.

Run the worker via::

    celery -A app.workers.celery_app worker

Configuration rationale
-----------------------
- JSON-only serialisation (no pickle) — eliminates the deserialise-RCE class
  of vulnerabilities; tasks must accept plain JSON-friendly payloads.
- ``task_acks_late=True`` + ``worker_prefetch_multiplier=1`` — at-least-once
  delivery, no hoarding.  Idempotency is the task author's responsibility;
  see .claude/skills/tasks/SKILL.md.

Task discovery
--------------
``autodiscover_tasks`` walks the listed packages and imports any ``tasks``
module it finds.  Domain modules add their tasks by creating
``app/core/<domain>/tasks.py``; nothing else needs touching here.
"""

from celery import Celery
from celery.signals import celeryd_init
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.sentry import init_sentry

_settings = get_settings()


@setup_logging.connect
def _configure_worker_logging(**_kwargs: object) -> None:
    """Run worker/beat logs through the same structlog pipeline as the API.

    Connecting `setup_logging` stops Celery from installing its own root
    handler; `LOG_LEVEL`/`LOG_FORMAT` drive the config — the CLI
    ``--loglevel`` flag is ignored.
    """
    configure_logging(get_settings())


app = Celery(
    "ai_red_teaming",
    broker=_settings.broker_url,
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Task lifecycle events on the broker — consumed by celery-exporter
    # (Prometheus task metrics) and Flower alike.
    worker_send_task_events=True,
    task_send_sent_event=True,
)


@celeryd_init.connect
def _init_worker_sentry(**_kwargs: object) -> None:
    """Initialize Sentry in the worker process (a no-op without `SENTRY_DSN`).

    Scoped to `celeryd_init` (worker-process startup) rather than called at
    module scope: `app/main.py` also imports this module to register the
    Celery app, and beat imports it too — neither should share the worker's
    Sentry init call.
    """
    init_sentry(_settings)


# Periodic tasks (run by `celery -A app.workers.celery_app beat`). Entries reference a
# task by its registered name so the schedule stays decoupled from import order.
app.conf.beat_schedule = {
    "reap-orphaned-streaming-messages": {
        "task": "app.core.conversations.tasks.reap_orphaned_streaming_messages",
        "schedule": float(_settings.streaming_reap_interval_seconds),
    },
    "reap-expired-export-jobs": {
        "task": "app.core.exports.tasks.reap_expired_export_jobs",
        "schedule": float(_settings.export_reap_interval_seconds),
    },
    "reap-orphaned-media": {
        "task": "app.core.media.tasks.reap_orphaned_media",
        "schedule": float(_settings.media_orphan_reap_interval_seconds),
    },
    "check-model-inactivity": {
        "task": "app.core.ai_gateway.tasks.check_model_inactivity",
        "schedule": float(_settings.model_inactivity_check_interval_seconds),
    },
}

app.autodiscover_tasks(
    [
        "app.core.auth",
        "app.core.conversations",
        "app.core.evaluations",
        "app.core.exports",
        "app.core.annotations",
        "app.core.analytics",
        "app.core.email",
        "app.core.ai_gateway",
        "app.core.media",
        "app.workers",
    ]
)
