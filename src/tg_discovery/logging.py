"""Structured logging with secret redaction."""

import logging
import re
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "***"
_SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "credential")
# A credential written inside a text: ``api_key=abc`` in a URL, ``token: abc``, ``Authorization: Bearer abc``.
_SECRET_IN_TEXT = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|credential)[\"'=:\s]+)(?!bearer\b)([^\s&\"',;]+)"
)
_BEARER_IN_TEXT = re.compile(r"(?i)(bearer\s+)[^\s\"',;]+")
# Libraries that log every request line with its full URL, which is where a key in the query string would end up.
_CHATTY_LOGGERS = ("httpx", "httpcore")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def mask_text(text: str) -> str:
    """Hide the credential in a text such as a logged URL: ``...?api_key=abc&q=x`` becomes ``...?api_key=***&q=x``."""
    return _BEARER_IN_TEXT.sub(rf"\1{REDACTED}", _SECRET_IN_TEXT.sub(rf"\1{REDACTED}", text))


def _redact(value: object) -> object:
    """Mask credential-looking values inside nested mappings and lists, and credentials written inside a text."""
    if isinstance(value, str):
        return mask_text(value)
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
    for name in _CHATTY_LOGGERS:  # their request lines print the whole URL, and the run's own log lines say it better
        logging.getLogger(name).setLevel(logging.DEBUG if level == "DEBUG" else logging.WARNING)
