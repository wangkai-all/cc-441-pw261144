"""Mixed-timezone timestamp parsing.

Log files in the wild mix ISO-8601 with offsets, ``Z`` suffixes, naive
local timestamps, logback-style comma millis and syslog headers. Every
timestamp is normalised to UTC epoch milliseconds for the timeline; the
original string and whether the timezone was assumed are preserved.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Prefix patterns (checked at the *start* of a line)
# ---------------------------------------------------------------------------

# 2026-11-18T10:20:30.123+08:00 | 2026-11-18 10:20:30,123 | ... Z | ... +0800
_ISO_PREFIX = re.compile(
    r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?"
    r"(?:\s?(?:Z|z|[+-]\d{2}:?\d{2}(?::\d{2})?))?)"
    r"\]?\s*"
)

# Nov 18 10:20:30  (syslog, no year, no tz)
_SYSLOG_PREFIX = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s"
)

# 18/Nov/2026:10:20:30 +0800  (apache/nginx)
_APACHE_PREFIX = re.compile(
    r"^\[?(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s+[+-]\d{4})\]?\s*"
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _to_epoch_ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))


def parse_timestamp(
    raw: str,
    default_tz: timezone = timezone.utc,
    now: Optional[datetime] = None,
) -> Optional[Tuple[int, bool]]:
    """Parse a timestamp string into (epoch_ms_utc, tz_assumed).

    Returns None when the string matches no known format. Naive timestamps
    are interpreted in ``default_tz`` and flagged ``tz_assumed=True``.
    """
    s = raw.strip()
    if not s:
        return None

    # ISO-8601 family (fromisoformat in 3.11+ accepts 'Z' and space separator)
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s):
        cand = re.sub(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}),(\d)",
                      r"\1.\2", s)  # logback comma-millis
        cand = re.sub(r"\s+(Z|z|[+-]\d{2}:?\d{2}(?::\d{2})?)$", r"\1",
                      cand)  # "....,000 -0500" -> offset attaches to the time
        try:
            dt = datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                return _to_epoch_ms(dt.replace(tzinfo=default_tz)), True
            return _to_epoch_ms(dt.astimezone(timezone.utc)), False
        except ValueError:
            pass

    # syslog: "Nov 18 10:20:30" - year inferred from *now*
    m = re.match(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$", s)
    if m and m.group(1) in _MONTHS:
        ref = now or datetime.now(default_tz)
        dt = datetime(ref.year, _MONTHS[m.group(1)], int(m.group(2)),
                      int(m.group(3)), int(m.group(4)), int(m.group(5)),
                      tzinfo=default_tz)
        if dt - ref > timedelta(days=1):  # dated in the future -> previous year
            dt = dt.replace(year=dt.year - 1)
        return _to_epoch_ms(dt), True

    # apache: "18/Nov/2026:10:20:30 +0800"
    m = re.match(r"^(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\s+([+-])(\d{2})(\d{2})$", s)
    if m and m.group(2) in _MONTHS:
        sign = 1 if m.group(7) == "+" else -1
        off = timedelta(hours=int(m.group(8)), minutes=int(m.group(9))) * sign
        dt = datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)),
                      int(m.group(4)), int(m.group(5)), int(m.group(6)),
                      tzinfo=timezone(off))
        return _to_epoch_ms(dt.astimezone(timezone.utc)), False

    return None


def extract_prefix_timestamp(
    line: str,
    default_tz: timezone = timezone.utc,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Extract a leading timestamp from a log line.

    Returns dict(raw=..., ts_utc=epoch_ms, tz_assumed=bool, rest=str) or None.
    """
    for rx in (_ISO_PREFIX, _SYSLOG_PREFIX, _APACHE_PREFIX):
        m = rx.match(line)
        if not m:
            continue
        raw = m.group("ts")
        parsed = parse_timestamp(raw, default_tz=default_tz, now=now)
        if parsed is None:
            continue
        ts_utc, assumed = parsed
        return {
            "raw": raw,
            "ts_utc": ts_utc,
            "tz_assumed": assumed,
            "rest": line[m.end():],
        }
    return None
