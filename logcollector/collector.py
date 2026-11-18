"""File collector: byte offsets, rotation detection, atomic checkpoints.

Reads watched files incrementally, emits :class:`RawLine` records into a
per-file assembler, and persists *all* state - read offset, line counter,
partial (newline-less) tail buffer and the assembler's unclosed event - in
an atomically replaced JSON checkpoint. A killed collector resumes exactly
where it stopped; an event split across a restart is stitched together.

Rotation handling:
* path reopened with a different inode  -> new generation, offset reset,
  the old generation's open event is flushed (``incomplete``) so it is
  never lost;
* file shrank below the stored offset  -> copy-truncate, same treatment.

Duplicate safety: the collector never re-reads below its checkpointed
offset, and event ids are content-derived, so reprocessing is idempotent.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .assembler import AssemblerConfig, EventAssembler, RawLine
from .store import ErrorStore, EventStore

CHECKPOINT_VERSION = 1


@dataclass
class Config:
    max_line_bytes: int = 16 * 1024     # longer physical lines are truncated
    max_event_lines: int = 200
    max_json_lines: int = 500
    json_strict: bool = True
    default_tz_name: str = "UTC"
    default_tz_offset_min: int = 0      # used when tz_name == "offset"
    read_chunk_bytes: int = 1 << 20     # per poll per file

    def assembler_config(self) -> AssemblerConfig:
        tz = timezone.utc
        if self.default_tz_name == "offset":
            tz = timezone(timedelta(minutes=self.default_tz_offset_min))
        return AssemblerConfig(
            default_tz=tz,
            max_event_lines=self.max_event_lines,
            max_json_lines=self.max_json_lines,
            json_strict=self.json_strict,
        )


@dataclass
class FileState:
    inode: int
    generation: int
    offset: int = 0            # next byte position to read
    line_no: int = 0           # lines fully consumed (1-based counter)
    partial: bytes = b""       # newline-less tail already read
    partial_offset: int = 0    # file offset of partial[0]

    def to_dict(self) -> dict:
        return {
            "inode": self.inode,
            "generation": self.generation,
            "offset": self.offset,
            "line_no": self.line_no,
            "partial_latin1": self.partial.decode("latin1"),
            "partial_offset": self.partial_offset,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileState":
        return cls(
            inode=d["inode"],
            generation=d["generation"],
            offset=d["offset"],
            line_no=d["line_no"],
            partial=d.get("partial_latin1", "").encode("latin1"),
            partial_offset=d.get("partial_offset", d["offset"]),
        )


class LogCollector:
    def __init__(self, paths: List[str], state_dir: str,
                 config: Optional[Config] = None):
        self.cfg = config or Config()
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.events = EventStore(os.path.join(state_dir, "events.jsonl"))
        self.errors = ErrorStore(os.path.join(state_dir, "errors.jsonl"))
        self.checkpoint_path = os.path.join(state_dir, "checkpoint.json")
        self._lock = threading.Lock()
        self._watch_globs = list(paths)
        self._files: Dict[str, FileState] = {}
        self._assemblers: Dict[str, EventAssembler] = {}
        self._missing: Dict[str, bool] = {}
        self.last_poll_ms: Optional[int] = None
        self.poll_count = 0
        self._load_checkpoint()

    # -- path resolution -------------------------------------------------

    def _resolve_paths(self) -> List[str]:
        out: List[str] = []
        for p in self._watch_globs:
            if os.path.isdir(p):
                out.extend(sorted(glob.glob(os.path.join(p, "*.log"))))
            elif any(c in p for c in "*?["):
                out.extend(sorted(glob.glob(p)))
            else:
                out.append(p)
        # stable de-dup preserving order
        seen, uniq = set(), []
        for p in out:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                uniq.append(ap)
        return uniq

    # -- checkpoint ------------------------------------------------------

    def _load_checkpoint(self) -> None:
        if not os.path.exists(self.checkpoint_path):
            return
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return  # corrupt checkpoint: start clean rather than crash
        if data.get("version") != CHECKPOINT_VERSION:
            return
        acfg = self.cfg.assembler_config()
        for path, fd in data.get("files", {}).items():
            self._files[path] = FileState.from_dict(fd)
        for path, ad in data.get("assemblers", {}).items():
            self._assemblers[path] = EventAssembler.from_state(ad, acfg)

    def save_checkpoint(self) -> None:
        data = {
            "version": CHECKPOINT_VERSION,
            "saved_ms": int(time.time() * 1000),
            "files": {p: fs.to_dict() for p, fs in self._files.items()},
            "assemblers": {p: a.dump_state()
                           for p, a in self._assemblers.items()},
        }
        tmp = self.checkpoint_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.checkpoint_path)  # atomic on POSIX

    # -- polling ---------------------------------------------------------

    def poll(self) -> dict:
        """Read new bytes from every watched file. Returns a summary."""
        with self._lock:
            self.poll_count += 1
            self.last_poll_ms = int(time.time() * 1000)
            summary: Dict[str, dict] = {}
            for path in self._resolve_paths():
                summary[path] = self._poll_file(path)
            self.save_checkpoint()
            return {
                "poll_ms": self.last_poll_ms,
                "files": summary,
                "events_total": len(self.events),
                "errors_unresolved": len(self.errors.unresolved()),
            }

    def _assembler_for(self, path: str, generation: int) -> EventAssembler:
        asm = self._assemblers.get(path)
        if asm is None or asm.generation != generation:
            asm = EventAssembler(path, generation, self.cfg.assembler_config())
            self._assemblers[path] = asm
        return asm

    def _route(self, outcomes: List[Tuple[str, dict]],
               stats: dict) -> None:
        for kind, payload in outcomes:
            if kind == "event":
                if self.events.add(payload):
                    stats["events"] += 1
            else:
                if self.errors.add(payload):
                    stats["errors"] += 1

    def _poll_file(self, path: str) -> dict:
        stats = {"events": 0, "errors": 0, "bytes": 0, "lines": 0,
                 "rotated": False, "status": "ok"}
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._missing[path] = True
            stats["status"] = "missing"
            return stats
        self._missing[path] = False

        fs = self._files.get(path)
        if fs is None:
            fs = FileState(inode=st.st_ino, generation=0)
            self._files[path] = fs
        elif fs.inode != st.st_ino:
            self._flush_path(path, "rotation", stats)
            fs = FileState(inode=st.st_ino, generation=fs.generation + 1)
            self._files[path] = fs
            stats["rotated"] = True
        elif st.st_size < fs.offset:
            self._flush_path(path, "truncation", stats)
            fs = FileState(inode=st.st_ino, generation=fs.generation + 1)
            self._files[path] = fs
            stats["rotated"] = True

        if st.st_size == fs.offset and not stats["rotated"]:
            return stats

        with open(path, "rb") as fh:
            fh.seek(fs.offset)
            data = fh.read(self.cfg.read_chunk_bytes)
        if not data:
            return stats
        stats["bytes"] = len(data)

        buf = fs.partial + data
        base = fs.partial_offset
        asm = self._assembler_for(path, fs.generation)
        parts = buf.split(b"\n")
        for chunk in parts[:-1]:
            raw = chunk[:-1] if chunk.endswith(b"\r") else chunk
            line_no = fs.line_no + 1
            truncated = len(raw) > self.cfg.max_line_bytes
            kept = raw[: self.cfg.max_line_bytes] if truncated else raw
            try:
                text = kept.decode("utf-8")
                decode_errors = False
            except UnicodeDecodeError:
                text = kept.decode("utf-8", "replace")
                decode_errors = True
            rl = RawLine(
                source=path,
                generation=fs.generation,
                line_no=line_no,
                offset=base,
                text=text,
                byte_len=len(raw),
                truncated=truncated,
                orig_len=len(raw) if truncated else None,
                decode_errors=decode_errors,
            )
            self._route(asm.feed(rl), stats)
            fs.line_no = line_no
            stats["lines"] += 1
            base += len(chunk) + 1
        fs.partial = parts[-1]
        fs.partial_offset = base
        fs.offset = base + len(parts[-1])
        return stats

    def _flush_path(self, path: str, reason: str, stats: Optional[dict] = None
                    ) -> List[Tuple[str, dict]]:
        """Emit the unread partial tail and force-close the open event."""
        stats = stats if stats is not None else {"events": 0, "errors": 0}
        outcomes: List[Tuple[str, dict]] = []
        fs = self._files.get(path)
        asm = self._assemblers.get(path)
        if fs is not None and fs.partial and asm is not None:
            raw = fs.partial
            truncated = len(raw) > self.cfg.max_line_bytes
            kept = raw[: self.cfg.max_line_bytes] if truncated else raw
            rl = RawLine(
                source=path,
                generation=fs.generation,
                line_no=fs.line_no + 1,
                offset=fs.partial_offset,
                text=kept.decode("utf-8", "replace"),
                byte_len=len(raw),
                truncated=truncated,
                orig_len=len(raw) if truncated else None,
                partial=True,
            )
            outcomes.extend(asm.feed(rl))
            fs.partial = b""
        if asm is not None:
            outcomes.extend(asm.flush(reason))
        self._route(outcomes, stats)
        return outcomes

    def flush_all(self, reason: str = "shutdown") -> dict:
        """Force-close every open event (kept in the store, not lost)."""
        with self._lock:
            stats = {"events": 0, "errors": 0}
            for path in list(self._assemblers):
                self._flush_path(path, reason, stats)
            self.save_checkpoint()
            return stats

    # -- introspection ---------------------------------------------------

    def status(self) -> dict:
        files = []
        for path in self._resolve_paths():
            fs = self._files.get(path)
            asm = self._assemblers.get(path)
            try:
                size = os.path.getsize(path)
                inode = os.stat(path).st_ino
            except OSError:
                size = None
                inode = None
            entry = {
                "path": path,
                "missing": self._missing.get(path, False),
                "inode": inode,
                "size": size,
                "generation": fs.generation if fs else None,
                "offset": fs.offset if fs else 0,
                "lines_read": fs.line_no if fs else 0,
                "partial_bytes": len(fs.partial) if fs else 0,
                "open_event": None,
            }
            if asm and asm.open_lines:
                first = asm.open_lines[0]
                entry["open_event"] = {
                    "mode": asm.mode,
                    "lines": len(asm.open_lines),
                    "since_line": first.line_no,
                    "since_offset": first.offset,
                    "preview": first.text[:120],
                }
            files.append(entry)
        return {
            "files": files,
            "events_total": len(self.events),
            "errors_total": len(self.errors),
            "errors_unresolved": len(self.errors.unresolved()),
            "last_poll_ms": self.last_poll_ms,
            "poll_count": self.poll_count,
            "config": {
                "max_line_bytes": self.cfg.max_line_bytes,
                "max_event_lines": self.cfg.max_event_lines,
                "max_json_lines": self.cfg.max_json_lines,
                "json_strict": self.cfg.json_strict,
                "default_tz": self.cfg.default_tz_name,
            },
        }
