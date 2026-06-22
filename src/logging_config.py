"""Centralized structlog configuration.

Call configure_logging() ONCE at the entry point (pipeline.py main or server.py main).
Every other module does: `import structlog; log = structlog.get_logger(__name__)`.
"""
from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging() -> None:
    """Configure structlog + stdlib logging for the whole process.

    - Env `LOG_LEVEL` (default INFO)
    - Env `ENV=prod` → JSON renderer (one event per line, aggregator-friendly)
    - Otherwise → colorized console renderer (dev-friendly)
    """
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    # 1. Configure stdlib so libraries that use `logging` directly (openai, google-auth,
    #    apscheduler) get routed through the same handler.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # 2. Configure structlog.
    is_prod = os.getenv("ENV", "dev").lower() == "prod"

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,        # pulls in bound context
        structlog.processors.add_log_level,             # adds `level` key
        structlog.processors.TimeStamper(fmt="iso"),    # adds `timestamp` key
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,           # renders `exc_info` nicely
    ]

    if is_prod:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
