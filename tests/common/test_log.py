"""Issue #9 — the log formatter must emit what the call site passed.

Every test here asserts on the **formatted output, parsed as JSON**. That is
the whole point of the issue: the previous test asserted ``hasattr(record,
"event_id")``, which was true while the emitted line contained no such field,
so a correlation id nobody could actually read looked fully tested.

``event_id`` is the only identifier that spans ingest -> topic -> processor. If
the formatter drops it, a delivery cannot be traced end to end, and the failure
is invisible: the code reads as though it logs one.
"""

from __future__ import annotations

import io
import json
import logging

import pytest


@pytest.fixture
def emit():
    """Log through a real handler and return the emitted line, parsed.

    A real ``StreamHandler`` rather than ``caplog``: caplog captures records
    before formatting, which is exactly the blind spot that let this bug ship.
    """
    from common.log import JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("test.emit")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    def _emit(level: str, message: str, *args, **kwargs) -> dict:
        getattr(logger, level)(message, *args, **kwargs)
        lines = [line for line in stream.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected exactly one line, got {lines}"
        return json.loads(lines[0])

    yield _emit
    logger.handlers.clear()


# -- the fields a caller passes ------------------------------------------------


def test_extra_fields_are_emitted(emit):
    """The bug: `extra=` set the attribute and the formatter dropped it."""
    payload = emit("info", "event published", extra={"event_id": "abc123", "message_id": "7"})

    assert payload["event_id"] == "abc123"
    assert payload["message_id"] == "7"


def test_a_record_with_no_extras_formats_without_error(emit):
    """Most records have no extras — uvicorn's, every library's. A format
    string naming `%(event_id)s` would raise on all of them."""
    payload = emit("info", "starting up")

    assert payload["message"] == "starting up"
    assert payload["severity"] == "INFO"
    assert payload["logger"] == "test.emit"


def test_percent_style_interpolation_still_works(emit):
    """`retry.py` logs with %-args. The formatter must emit the interpolated
    message, not the raw template — `record.msg` is not `record.getMessage()`."""
    payload = emit("warning", "publish attempt %d/%d failed", 2, 3)

    assert payload["message"] == "publish attempt 2/3 failed"


def test_severity_is_the_key_cloud_logging_reads(emit):
    """Cloud Logging maps `severity` specifically. Under any other key it
    files every line as DEFAULT and severity-based alerting silently stops."""
    payload = emit("error", "publish failed")

    assert payload["severity"] == "ERROR"


# -- the JSON has to be valid JSON ---------------------------------------------


def test_a_quote_in_the_message_does_not_break_the_json(emit):
    """The second bug in the same line: JSON built by string interpolation.

    A GCP error message routinely contains the quoted topic path, so this hits
    exactly the ERROR line you most wanted to read.
    """
    payload = emit("error", 'NotFound: topic "webhook-events" does not exist')

    assert payload["message"] == 'NotFound: topic "webhook-events" does not exist'


def test_backslashes_and_newlines_survive(emit):
    payload = emit("warning", "path C:\\temp\\x and\na second line")

    assert payload["message"] == "path C:\\temp\\x and\na second line"


def test_a_quote_in_an_extra_value_does_not_break_the_json(emit):
    payload = emit("info", "ok", extra={"detail": 'he said "hello"'})

    assert payload["detail"] == 'he said "hello"'


def test_a_non_serializable_extra_does_not_crash_logging(emit):
    """Logging must never be the thing that takes the service down."""

    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    payload = emit("info", "ok", extra={"thing": Opaque()})

    assert payload["thing"] == "<Opaque>"


# -- exceptions ----------------------------------------------------------------


def test_exception_info_is_a_field_not_appended_text(emit):
    """`logging.Formatter` appends the traceback after the formatted line,
    which would put raw multi-line text outside the JSON object."""
    try:
        raise ValueError("topic vanished")
    except ValueError:
        payload = emit("error", "publish failed", exc_info=True)

    assert "exception" in payload, "the traceback should be its own field"
    assert "ValueError" in payload["exception"]
    assert "topic vanished" in payload["exception"]
    assert payload["message"] == "publish failed", "the message stays the message"


# -- what must NOT be emitted ---------------------------------------------------


def test_logging_machinery_is_not_dumped_into_the_payload(emit):
    """`record.__dict__` holds ~20 internal attributes. Emitting them all
    would put absolute source paths and process ids on every line, and bury
    the two fields anyone actually queries."""
    payload = emit("info", "ok", extra={"event_id": "abc"})

    for internal in ("args", "levelno", "pathname", "msecs", "relativeCreated", "exc_info"):
        assert internal not in payload, f"{internal} is logging machinery, not a field"


def test_an_extra_cannot_overwrite_the_severity(emit):
    """`logging` protects its own reserved names but knows nothing about
    `severity`, so without a guard a caller could silently downgrade a line."""
    payload = emit("error", "publish failed", extra={"severity": "INFO"})

    assert payload["severity"] == "ERROR"


def test_output_is_exactly_one_line_per_record(emit):
    """A record split across lines is a record Cloud Logging reads as two."""
    payload = emit("error", "line one\nline two")

    assert payload["message"] == "line one\nline two"


# -- configure_logging ----------------------------------------------------------


def test_configure_logging_installs_the_json_formatter():
    from common.log import JsonFormatter, configure_logging

    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        configure_logging("DEBUG")

        assert root.handlers, "configure_logging should install a handler"
        assert all(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


def test_configure_logging_routes_uvicorn_through_the_same_formatter():
    """uvicorn installs plain-text handlers of its own. Left alone, half the
    container's output is unstructured and the access log is unqueryable."""
    from common.log import configure_logging

    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler())  # stand in for uvicorn's own

    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        configure_logging("INFO")

        assert not access.handlers, "uvicorn's own handlers should be removed"
        assert access.propagate, "so its records reach the JSON handler on root"
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        access.handlers.clear()
