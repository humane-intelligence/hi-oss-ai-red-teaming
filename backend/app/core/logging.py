"""Structlog configuration — unifies stdlib + structlog into a single pipeline."""

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict
from structlog.typing import Processor

from app.core.config import Settings

_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]


def _add_service(service: str) -> Processor:
    def processor(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service
        return event_dict

    return processor


def _render_chain(settings: Settings) -> list[Processor]:
    # ConsoleRenderer formats exc_info itself (pretty, colored); JSONRenderer
    # can't serialize the raw tuple, so it needs format_exc_info first.
    # JSON output follows the log-shipping convention (Loki/Promtail): the
    # message key is `message` (not structlog's `event`) and every line
    # carries `service`; console output keeps structlog's native shape.
    if settings.log_format == "json":
        return [
            _add_service(settings.service_name),
            structlog.processors.EventRenamer("message"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    # Default RichTracebackFormatter dumps every frame's locals, which turns a single
    # exception crossing FastAPI/SQLAlchemy/asyncpg internals into thousands of lines.
    exception_formatter = structlog.dev.RichTracebackFormatter(show_locals=False)
    return [structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty(), exception_formatter=exception_formatter)]


def configure_logging(settings: Settings) -> None:
    """Wire structlog and stdlib logging into a single pipeline.

    Installs a single ``StreamHandler`` on the root logger that runs every
    record — structlog or stdlib — through the same processor chain. Picks
    the renderer (``JSONRenderer`` vs ``ConsoleRenderer``) from
    ``settings.log_format`` and the root level from ``settings.log_level``.
    Disables uvicorn's access log so `LoggingMiddleware` is the sole source
    of access records (otherwise every request would be logged twice).

    Idempotent — safe to call again with a different `Settings` (e.g. in
    tests); replaces the root handler in place.

    Args:
        settings: Application settings; ``log_level`` and ``log_format``
            drive the configuration.
    """
    render_chain = _render_chain(settings)

    structlog.configure(
        processors=[*_SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *render_chain],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn ships its own handlers; clear them so records propagate to root.
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True

    # LoggingMiddleware emits access logs; silence uvicorn's to avoid duplicates.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog `BoundLogger` for the calling module.

    Thin wrapper around `structlog.stdlib.get_logger` so callers depend only
    on `app.core.logging` instead of importing structlog directly. Pass
    ``__name__`` so each module's records carry their dotted path.

    Args:
        name: Logger name; conventionally ``__name__``. If ``None``, the
            structlog root logger is returned.
        **initial_values: Key/value pairs bound onto the returned logger,
            so every record it emits carries them.

    Returns:
        A `BoundLogger` ready for ``.info()`` / ``.warning()`` calls.
    """
    return structlog.stdlib.get_logger(name, **initial_values)
