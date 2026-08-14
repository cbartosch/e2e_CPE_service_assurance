"""Structured logging, with redaction wired into the pipeline rather than left to callers.

**The load-bearing part of this module is `_redact_processor`.** A redaction step that callers must
remember to invoke is a redaction step that will be forgotten -- not by the person who wrote this
module, but by the person adding a debug line to a node at 02:00 during an incident, which is
exactly the moment the payload is richest and the log most likely to be read by someone outside the
team. So `security.redaction.redact` runs over every event dict on its way to the renderer,
unconditionally. A caller *cannot* emit an unmasked MAC, e-mail or phone number through this logger,
whatever they pass.

That is a second line of defence, not the first: masking belongs at the collection boundary (see
`security.redaction`'s module docstring) because state is checkpointed before any log line is
written. Both exist because either alone leaves a hole.

Two renderers: JSON for anything shipped to a log aggregator, console for a developer's terminal.
Chosen from `settings.log_format`, never from a caller argument, so a production deployment cannot
end up emitting console-coloured escape codes into a JSON pipeline.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import Any, Final

import structlog
from structlog.types import EventDict, FilteringBoundLogger, WrappedLogger

from lpr_cpe.config.settings import LogFormat, Settings, get_settings
from lpr_cpe.security.redaction import redact

# Whether `configure_logging` has already run, and what it ran with. `configure_logging` is called
# from the API's lifespan, from the CLI, and defensively from a few entry points that can be run
# standalone -- so it WILL be called more than once in one process.
#
# Idempotency matters for a specific, observed reason: uvicorn's `--reload` supervisor imports the
# application module in the parent process and again in the worker, and `structlog.configure` with
# `cache_logger_on_first_use=True` silently does nothing to loggers that were already cached. A
# second call that appended to the processor chain would double every log line; a second call that
# reset the chain would drop the context bound before it. Neither is visible in a test that only
# calls it once, which is why the guard is here rather than in each caller.
_configured: bool = False
_configured_with: tuple[str, str] | None = None

_HANDLER_TAG: Final = "lpr_cpe.observability"

# Keys never masked, because they are ours and are not customer data. `redact` would leave most of
# them alone anyway, but `logger` and `event` are free text and the shape-based masker could in
# principle alter a message that merely looks like an identifier -- and a log line whose *event
# name* has been rewritten is a log line that cannot be grepped for.
_PASSTHROUGH_KEYS: Final[frozenset[str]] = frozenset({"level", "timestamp", "logger", "log_level"})


def _redact_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Mask customer-identifying values in every log event. Installed, not optional.

    Runs late in the chain -- after `format_exc_info`, so a traceback that quotes a MAC is masked
    too -- and returns a new mapping rather than editing in place, because the values in
    `event_dict` are often the caller's live objects.

    The `event` message itself IS masked (by shape, so only MAC/e-mail/phone/IP-looking substrings
    change). A f-string message is the commonest accidental PII leak in any codebase, and exempting
    the message would leave the biggest hole open.
    """
    out: EventDict = {}
    for key, value in event_dict.items():
        if key in _PASSTHROUGH_KEYS:
            out[key] = value
        else:
            out[key] = redact(value)
    return out


def _level_number(level_name: str) -> int:
    """`"INFO"` -> `logging.INFO`. `Settings` has already validated the name."""
    return int(getattr(logging, level_name.upper(), logging.INFO))


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Install the structlog pipeline. Idempotent.

    Repeat calls with the same `(log_format, log_level)` are no-ops. A repeat call with *different*
    values reconfigures, because that is a test deliberately switching format and it should work.
    `force=True` reconfigures unconditionally, which is what `tests/conftest.py` needs.

    Renderer choice comes from `settings.log_format`. The stdlib root logger is given a handler only
    if it has none, so running under uvicorn (which installs its own) does not produce every line
    twice.
    """
    global _configured, _configured_with

    resolved = settings if settings is not None else get_settings()
    signature = (resolved.log_format.value, resolved.log_level)
    if _configured and not force and _configured_with == signature:
        return

    level = _level_number(resolved.log_level)

    shared: list[Any] = [
        # `merge_contextvars` first: context bound by the API middleware (correlation id) must be
        # present for every processor that follows, including redaction.
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        # Redaction is the last thing before rendering. Anything added after this line is outside
        # the masking boundary; there is deliberately nothing after it but the renderer.
        _redact_processor,
    ]

    renderer: Any
    if resolved.log_format is LogFormat.JSON:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(level)
    # A handler only when the root logger has none. Under uvicorn, or under pytest's `caplog`,
    # something else has already installed one and adding a second emits every line twice. An empty
    # `handlers` list also implies ours is not there, so no separate "have we already?" test is
    # needed -- the tag exists so a test or an operator can identify our handler among others, not
    # to guard this branch.
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        # The message is already fully rendered by structlog; a stdlib format string here would wrap
        # a JSON line in another layer of prefix and break every downstream parser.
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._lpr_tag = _HANDLER_TAG  # type: ignore[attr-defined]
        root.addHandler(handler)

    _configured = True
    _configured_with = signature


def is_configured() -> bool:
    """Whether `configure_logging` has run here. For tests and for the readiness probe."""
    return _configured


def reset_logging_for_tests() -> None:
    """Forget that configuration happened, without touching structlog's own state.

    Exists so a test can assert that `configure_logging` is idempotent *and* that it does something
    on the first call. Not for production use.
    """
    global _configured, _configured_with
    _configured = False
    _configured_with = None


def get_logger(name: str) -> FilteringBoundLogger:
    """A logger for `name`, configuring logging on first use if nobody has yet.

    The lazy configuration is deliberate. A module-level `logger = get_logger(__name__)` in a node
    runs at import time, which may be before the API's lifespan has started; without this the first
    few lines would go out through structlog's default (unredacted) pipeline. Configuring here means
    there is no window in which an unmasked line can be emitted.
    """
    if not _configured:
        configure_logging()
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger


def bind_incident(
    logger: FilteringBoundLogger,
    state_or_ids: Mapping[str, Any],
) -> FilteringBoundLogger:
    """Bind `incident_id`, `correlation_id` and `thread_id` onto `logger`.

    Takes an `IncidentState` (a `TypedDict`, so a `Mapping` at runtime) or a plain dict of ids, so a
    node passes its state and the API passes what it parsed from the request.

    Only keys that are actually present and non-empty are bound. Binding `incident_id=None` produces
    a log line that looks attributed and is not, which is worse than an unattributed line: a search
    for "incidents with no id" would not find it.

    `thread_id` is bound even though D1 makes it equal to `incident_id`, because the LangGraph
    checkpoint tables are keyed on `thread_id` and an operator correlating a log line to a
    checkpoint row should not have to know the two are the same.
    """
    bound: dict[str, str] = {}
    for key in ("incident_id", "correlation_id", "thread_id"):
        value = state_or_ids.get(key)
        if isinstance(value, str) and value:
            bound[key] = value
    if not bound:
        return logger
    rebound: FilteringBoundLogger = logger.bind(**bound)
    return rebound


def bind_context(**values: str) -> None:
    """Bind values into the context-local store, so they appear on every later line in this task.

    Used by the API middleware for `correlation_id` and by the graph runner for `incident_id`. A
    context-var binding survives across `await` points within one task and does not leak into a
    sibling task, which is what makes it safe under concurrent incidents -- `logger.bind()` on a
    module-level logger would not be.
    """
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    """Drop everything bound by `bind_context`. Called at the end of a request or a graph run."""
    structlog.contextvars.clear_contextvars()
