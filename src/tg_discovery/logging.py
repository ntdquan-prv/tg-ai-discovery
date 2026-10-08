"""Structured logging with secret redaction."""

import logging
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "***"
_SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "credential")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact(value: object) -> object:
    """Mask credential-looking values inside nested mappings and lists."""
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {k: REDACTED if _is_sensitive(str(k)) else _redact(v) for k, v in mapping.items()}
    if isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        return [_redact(item) for item in items]
    return value


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Structlog processor that masks values whose key looks like a credential."""
    return {k: REDACTED if _is_sensitive(k) else _redact(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Route structlog and the standard library through one renderer (JSON in production)."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
