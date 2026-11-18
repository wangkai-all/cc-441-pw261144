"""Event assembly state machine.

One assembler per log file generation. It consumes :class:`RawLine` records
and emits completed events or quarantined parse errors (dead letters):

* multi-line exception stacks are folded into the preceding log event;
* pretty-printed / nested JSON is accumulated until the top-level value
  balances, then parsed (braces inside strings are handled);
* truncated and EOF-partial lines keep their flags on the event;
* timestamps are normalised to UTC, mixed timezones included;
* a line that starts a JSON value but never balances is quarantined as
  ``json-unclosed`` when the next timestamped line arrives, so one bad
  write never swallows the events that follow it.

The whole state (mode + accumulated lines) round-trips through
:meth:`dump_state`/:meth:`load_state` so a checkpointed collector resumes
mid-event after a crash.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import timezone
from typing import Any, List, Optional, Tuple

from .timestamps import extract_prefix_timestamp, parse_timestamp

LEVEL_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b", re.IGNORECASE
)
STACK_RE = re.compile(
    r"^\s*(?:at\s+[\w.$_]+\([\w.$_]*(?::\d+)?\)\s*$"      # Java frame
    r"|File\s+\"[^\"]+\",\s+line\s+\d+"                    # Python frame
    r"|Traceback \(most recent call last\):"
    r"|Caused by:"
    r"|\.\.\.\s+\d+\s+more"
    r"|raise\s+\w+)"
)
_TS_KEYS = ("timestamp", "ts", "time", "@timestamp", "datetime")
_LEVEL_KEYS = ("level", "severity", "log.level", "loglevel")
_MSG_KEYS = ("message", "msg", "log", "body")

MAX_SUMMARY = 300
MAX_EMBEDDED_JSON = 20_000  # chars of json.dumps() kept on the event


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def json_depth(text: str) -> Tuple[int, bool]:
    """Return (unclosed bracket depth, inside_string) for JSON-ish text."""
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
    return depth, in_str


def salvage_json(text: str) -> Optional[Any]:
    """Best-effort repair used by non-strict replay: strip trailing commas."""
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(repaired)
    except ValueError:
        return None


@dataclass
class RawLine:
    source: str
    generation: int
    line_no: int
    offset: int                      # byte offset of line start in the file
    text: str
    byte_len: int = -1               # bytes on disk (excl. newline); -1 -> derive
    truncated: bool = False          # line exceeded max_line_bytes, tail dropped
    orig_len: Optional[int] = None   # full byte length when truncated
    partial: bool = False            # terminated by EOF/flush, not a newline
    decode_errors: bool = False      # invalid UTF-8 replaced

    def __post_init__(self) -> None:
        if self.byte_len < 0:
            self.byte_len = len(self.text.encode("utf-8"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RawLine":
        return cls(**d)


@dataclass
class AssemblerConfig:
    default_tz: timezone = timezone.utc
    max_event_lines: int = 200       # runaway-stack guard
    max_json_lines: int = 500        # runaway-JSON guard
    json_strict: bool = True         # False -> salvage_json on parse failure
    max_summary: int = MAX_SUMMARY


class EventAssembler:
    """Per-file-generation assembly state machine."""

    def __init__(self, source: str, generation: int = 0,
                 config: Optional[AssemblerConfig] = None):
        self.source = source
        self.generation = generation
        self.cfg = config or AssemblerConfig()
        self.mode: Optional[str] = None          # None | "log" | "json"
        self.open_lines: List[RawLine] = []
        self._decoder = json.JSONDecoder()

    # -- checkpoint ------------------------------------------------------

    def dump_state(self) -> dict:
        return {
            "source": self.source,
            "generation": self.generation,
            "mode": self.mode,
            "open_lines": [l.to_dict() for l in self.open_lines],
        }

    @classmethod
    def from_state(cls, state: dict, config: Optional[AssemblerConfig] = None
                   ) -> "EventAssembler":
        asm = cls(state["source"], state["generation"], config)
        asm.mode = state["mode"]
        asm.open_lines = [RawLine.from_dict(d) for d in state["open_lines"]]
        return asm

    # -- feeding ---------------------------------------------------------

    def feed(self, raw: RawLine) -> List[Tuple[str, dict]]:
        """Consume one line; return list of ("event"|"error", payload)."""
        out: List[Tuple[str, dict]] = []

        if self.mode == "json":
            # A timestamped line while a JSON value is still unbalanced means
            # the writer abandoned the object mid-write (crash/rotation):
            # quarantine what we have, then treat this line normally.
            depth, in_str = json_depth("\n".join(l.text for l in self.open_lines))
            if (depth > 0 or in_str) and extract_prefix_timestamp(
                    raw.text, self.cfg.default_tz) is not None:
                out.append(("error", self._make_error(
                    "json-unclosed",
                    "JSON value never balanced before next timestamped line")))
                self._reset()
                # fall through and process `raw` as a fresh line
            else:
                self.open_lines.append(raw)
                self._json_check(out)
                return out

        stripped = raw.text.strip()
        if extract_prefix_timestamp(raw.text, self.cfg.default_tz) is not None:
            if self.mode == "log":
                ev = self._finalize_log_event()
                if ev:
                    out.append(("event", ev))
            self.mode = "log"
            self.open_lines = [raw]
            return out

        if self._looks_like_json_start(stripped):
            if self.mode == "log":
                ev = self._finalize_log_event()
                if ev:
                    out.append(("event", ev))
            self.mode = "json"
            self.open_lines = [raw]
            self._json_check(out)
            return out

        if self.mode == "log":
            self.open_lines.append(raw)
            if len(self.open_lines) >= self.cfg.max_event_lines:
                ev = self._finalize_log_event(overflow=True)
                if ev:
                    out.append(("event", ev))
            return out

        # Orphan line with no open event: keep it as its own event so
        # nothing is ever dropped silently.
        out.append(("event", self._orphan_event(raw)))
        return out

    def flush(self, reason: str) -> List[Tuple[str, dict]]:
        """Force-close any open event (rotation, truncation, shutdown)."""
        out: List[Tuple[str, dict]] = []
        if not self.open_lines:
            return out
        if self.mode == "json":
            err = self._make_error("json-unclosed", f"flushed: {reason}")
            err["flush_reason"] = reason
            out.append(("error", err))
        else:
            ev = self._finalize_log_event(incomplete=True, flush_reason=reason)
            if ev:
                out.append(("event", ev))
        self._reset()
        return out

    # -- internals -------------------------------------------------------

    @staticmethod
    def _looks_like_json_start(stripped: str) -> bool:
        """Heuristic gate so ``[INFO] msg`` is not mistaken for a JSON array.

        ``{`` always qualifies (trade-off: a message literally starting with
        ``{`` is quarantined as json-parse, replayable). ``[`` qualifies only
        when the next char can start a JSON value, so bracketed levels like
        ``[INFO]``/``[TRACE]`` stay plain log lines.
        """
        if not stripped:
            return False
        if stripped[0] == "{":
            return True
        if stripped[0] == "[":
            if len(stripped) == 1:
                return True
            return stripped[1] in '{["0123456789-tfn \t'
        return False

    def _reset(self) -> None:
        self.mode = None
        self.open_lines = []

    def _json_check(self, out: List[Tuple[str, dict]]) -> None:
        text = "\n".join(l.text for l in self.open_lines)
        depth, in_str = json_depth(text)
        if depth > 0 or in_str:
            if len(self.open_lines) >= self.cfg.max_json_lines:
                out.append(("error", self._make_error(
                    "json-overflow",
                    f"exceeded max_json_lines={self.cfg.max_json_lines}")))
                self._reset()
            return
        try:
            obj, end = self._decoder.raw_decode(text)
            rest = text[end:].strip()
            if rest:
                raise ValueError(f"trailing data after JSON value: {rest[:40]!r}")
            out.append(("event", self._finalize_json_event(obj, salvaged=False)))
        except (json.JSONDecodeError, ValueError) as exc:
            if not self.cfg.json_strict:
                salvaged = salvage_json(text)
                if salvaged is not None:
                    out.append(("event", self._finalize_json_event(salvaged, True)))
                    self._reset()
                    return
            out.append(("error", self._make_error("json-parse", str(exc))))
        self._reset()

    # -- event builders --------------------------------------------------

    def _base_event(self, lines: List[RawLine], kind: str) -> dict:
        first, last = lines[0], lines[-1]
        eid = _sha1(f"{first.source}|{first.generation}|{first.offset}|"
                    f"{first.line_no}|{first.text[:64]}")
        return {
            "event_id": eid,
            "source": first.source,
            "generation": first.generation,
            "kind": kind,
            "start_line": first.line_no,
            "end_line": last.line_no,
            "line_count": len(lines),
            "start_offset": first.offset,
            "end_offset": last.offset + last.byte_len + (0 if last.partial else 1),
            "lines": [{
                "line_no": l.line_no,
                "offset": l.offset,
                "byte_len": l.byte_len,
                **({"truncated": True, "orig_len": l.orig_len} if l.truncated else {}),
                **({"partial": True} if l.partial else {}),
                **({"decode_errors": True} if l.decode_errors else {}),
            } for l in lines],
            "truncated": any(l.truncated for l in lines),
            "partial": any(l.partial for l in lines),
            "ingest_ms": _now_ms(),
        }

    def _apply_ts(self, ev: dict, line_text: str) -> None:
        ts = extract_prefix_timestamp(line_text, self.cfg.default_tz)
        if ts:
            ev.update(ts_raw=ts["raw"], ts_utc=ts["ts_utc"],
                      tz_assumed=ts["tz_assumed"], time_source="line")
        else:
            ev.update(ts_raw=None, ts_utc=ev["ingest_ms"],
                      tz_assumed=False, time_source="ingest")

    def _finalize_log_event(self, overflow: bool = False,
                            incomplete: bool = False,
                            flush_reason: Optional[str] = None) -> Optional[dict]:
        lines, self.open_lines = self.open_lines, []
        self.mode = None
        if not lines:
            return None
        ev = self._base_event(lines, "log")
        first = lines[0].text
        self._apply_ts(ev, first)
        m = LEVEL_RE.search(first[:200])
        ev.update({
            "message": first[:self.cfg.max_summary],
            "level": (m.group(1).upper().replace("WARNING", "WARN") if m else None),
            "has_stack": any(STACK_RE.search(l.text) for l in lines),
        })
        if overflow:
            ev["overflow"] = True
        if incomplete:
            ev["incomplete"] = True
            ev["flush_reason"] = flush_reason
        return ev

    def _finalize_json_event(self, obj: Any, salvaged: bool) -> dict:
        lines, self.open_lines = self.open_lines, []
        self.mode = None
        ev = self._base_event(lines, "json")
        msg = level = None
        ts_utc = ts_raw = None
        tz_assumed = False
        if isinstance(obj, dict):
            for k in _MSG_KEYS:
                if isinstance(obj.get(k), str):
                    msg = obj[k]
                    break
            for k in _LEVEL_KEYS:
                if isinstance(obj.get(k), str):
                    level = obj[k].upper().replace("WARNING", "WARN")
                    break
            for k in _TS_KEYS:
                if k not in obj:
                    continue
                v = obj[k]
                if isinstance(v, str):
                    parsed = parse_timestamp(v, self.cfg.default_tz)
                    if parsed:
                        ts_utc, tz_assumed = parsed
                        ts_raw = v
                        break
                elif isinstance(v, (int, float)):
                    ts_utc = int(v * 1000) if v < 1e12 else int(v)
                    ts_raw = str(v)
                    break
        if ts_utc is None:
            # fall back to a timestamp prefix on the first physical line
            self._apply_ts(ev, lines[0].text)
            if ev.get("time_source") == "ingest":
                pass  # keep ingest time
        else:
            ev.update(ts_raw=ts_raw, ts_utc=ts_utc,
                      tz_assumed=tz_assumed, time_source="json")
        dumped = json.dumps(obj, ensure_ascii=False)
        ev.update({
            "message": (msg or dumped)[:self.cfg.max_summary],
            "level": level,
            "has_stack": False,
        })
        if len(dumped) <= MAX_EMBEDDED_JSON:
            ev["json"] = obj
        else:
            ev["json_omitted"] = True
            if isinstance(obj, dict):
                ev["json_keys"] = list(obj.keys())[:50]
        if salvaged:
            ev["json_salvaged"] = True
        return ev

    def _orphan_event(self, raw: RawLine) -> dict:
        ev = self._base_event([raw], "unstructured")
        self._apply_ts(ev, raw.text)
        m = LEVEL_RE.search(raw.text[:200])
        ev.update({
            "message": raw.text[:self.cfg.max_summary],
            "level": (m.group(1).upper().replace("WARNING", "WARN") if m else None),
            "has_stack": False,
        })
        return ev

    def _make_error(self, reason: str, detail: str) -> dict:
        lines = self.open_lines
        first, last = lines[0], lines[-1]
        return {
            "error_id": _sha1(f"err|{first.source}|{first.generation}|"
                              f"{first.offset}|{reason}"),
            "source": first.source,
            "generation": first.generation,
            "reason": reason,
            "detail": detail[:500],
            "start_line": first.line_no,
            "end_line": last.line_no,
            "start_offset": first.offset,
            "lines": [{
                "line_no": l.line_no,
                "offset": l.offset,
                "byte_len": l.byte_len,
                "text": l.text,
                "truncated": l.truncated,
                "partial": l.partial,
            } for l in lines],
            "detected_ms": _now_ms(),
            "resolved": False,
        }
