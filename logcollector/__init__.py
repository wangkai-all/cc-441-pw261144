"""logcollector - resilient multi-line log collector.

Assembles raw log lines (multi-line exception stacks, nested/pretty JSON,
truncated lines, mixed timezones) into events while preserving original
line numbers and byte offsets. Collector state (including unclosed events)
is checkpointed atomically, so an interrupted run resumes mid-event.
Parse failures are quarantined as replayable dead letters and never lose
subsequent events.
"""

from .timestamps import parse_timestamp, extract_prefix_timestamp
from .assembler import EventAssembler, RawLine
from .collector import LogCollector, Config
from .store import EventStore, ErrorStore
from .replay import replay_errors

__all__ = [
    "parse_timestamp",
    "extract_prefix_timestamp",
    "EventAssembler",
    "RawLine",
    "LogCollector",
    "Config",
    "EventStore",
    "ErrorStore",
    "replay_errors",
]

__version__ = "0.1.0"
