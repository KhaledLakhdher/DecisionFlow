"""Structured logging.

JSON in production so logs are machine-parseable by whatever ships them;
coloured key-value output in development so they are readable by a human.
"""

from __future__ import annotations

import logging
import sys

import structlog

from decisionflow.core.config import settings


def configure_logging() -> None:
    """Configure structlog and route stdlib logging through it.

    Safe to call more than once; later calls simply replace the configuration.
    """
    level = logging.DEBUG if settings.debug else logging.INFO

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Must be last: hands the event dict to the stdlib handler's
            # ProcessorFormatter rather than rendering it here. Rendering in
            # both places prints the timestamp and level twice.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # A single renderer for both structlog events and foreign records
    # (uvicorn, sqlalchemy, arq), so the output stream stays homogeneous.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn and arq both install their own handlers. Left in place, every
    # line they emit appears twice — once in their format, once in ours.
    # Clearing them and relying on propagation gives exactly one rendering.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq", "arq.worker"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # Third-party loggers that are unusable at their default verbosity. botocore
    # in particular emits dozens of DEBUG lines per S3 call, which buries the
    # worker's own output completely when DEBUG is on.
    for noisy in ("sqlalchemy.engine", "botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
