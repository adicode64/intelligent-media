"""
Structured logging setup.

JSON logs by default so that the API and the Celery worker emit machine-parseable
records with a stable shape. `bind()` returns a LoggerAdapter that injects
contextual fields (e.g. image_id) into every record.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, MutableMapping

from app.config import settings

_CONFIGURED = False


class _ContextAdapter(logging.LoggerAdapter):
    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        try:
            from pythonjsonlogger.json import JsonFormatter
        except ImportError:  # pragma: no cover - older lib layout
            try:
                from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore
            except ImportError:  # pragma: no cover - lib missing entirely
                JsonFormatter = None  # type: ignore[assignment]

        if JsonFormatter is not None:
            handler.setFormatter(
                JsonFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    rename_fields={"asctime": "timestamp", "levelname": "level"},
                )
            )
    if handler.formatter is None:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s :: %(message)s")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # Uvicorn access logs are noisy in JSON mode; keep them but at the same level.
    for noisy in ("uvicorn.access", "uvicorn.error", "celery"):
        logging.getLogger(noisy).propagate = True
        logging.getLogger(noisy).handlers = []

    _CONFIGURED = True


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    configure_logging()
    return _ContextAdapter(logging.getLogger(name), context)
