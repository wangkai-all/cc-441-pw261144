"""Replay of quarantined parse errors (dead letters).

Every dead letter stores its original raw lines (with line numbers and
offsets). Replay re-runs the *current* assembler over those lines - after a
parser fix or with relaxed config (``json_strict=False``, higher
``max_json_lines``) - and reports a per-error diff: recovered events vs.
still failing. Because replayed events keep their original source and
offsets, their ``event_id`` matches what the event would have had
originally, so applying a replay is idempotent in the event store.
"""
from __future__ import annotations

import time
from typing import Iterable, List, Optional

from .assembler import AssemblerConfig, EventAssembler, RawLine


def _summarize_event(ev: dict) -> dict:
    return {
        "event_id": ev["event_id"],
        "kind": ev["kind"],
        "start_line": ev["start_line"],
        "end_line": ev["end_line"],
        "start_offset": ev["start_offset"],
        "level": ev.get("level"),
        "message": (ev.get("message") or "")[:120],
        "json_salvaged": ev.get("json_salvaged", False),
    }


def replay_errors(errors: Iterable[dict],
                  base_config: Optional[AssemblerConfig] = None,
                  overrides: Optional[dict] = None,
                  replay_id: Optional[str] = None) -> dict:
    """Re-assemble each unresolved dead letter; return the replay diff."""
    overrides = overrides or {}
    cfg = base_config or AssemblerConfig()
    eff = AssemblerConfig(
        default_tz=cfg.default_tz,
        max_event_lines=int(overrides.get("max_event_lines", cfg.max_event_lines)),
        max_json_lines=int(overrides.get("max_json_lines", cfg.max_json_lines)),
        json_strict=bool(overrides.get("json_strict", cfg.json_strict)),
        max_summary=cfg.max_summary,
    )
    replay_id = replay_id or f"r-{int(time.time() * 1000)}"
    results: List[dict] = []

    for err in errors:
        if err.get("resolved"):
            continue
        asm = EventAssembler(err["source"], err["generation"], eff)
        outcomes = []
        for ld in err["lines"]:
            raw = RawLine(
                source=err["source"],
                generation=err["generation"],
                line_no=ld["line_no"],
                offset=ld["offset"],
                text=ld["text"],
                byte_len=ld.get("byte_len", -1),
                truncated=ld.get("truncated", False),
                partial=ld.get("partial", False),
            )
            outcomes.extend(asm.feed(raw))
        outcomes.extend(asm.flush(reason="replay-eof"))

        events = [p for kind, p in outcomes if kind == "event"]
        new_errors = [p for kind, p in outcomes if kind == "error"]
        status = "recovered" if events and not new_errors else "still_failing"
        results.append({
            "error_id": err["error_id"],
            "source": err["source"],
            "generation": err["generation"],
            "original_reason": err["reason"],
            "original_detail": err["detail"],
            "line_span": [err["start_line"], err["end_line"]],
            "status": status,
            "recovered_events": [_summarize_event(e) for e in events],
            "new_errors": [{"reason": e["reason"], "detail": e["detail"]}
                           for e in new_errors],
            "_events": events,  # full payloads for apply; stripped before API
        })

    recovered = [r for r in results if r["status"] == "recovered"]
    return {
        "replay_id": replay_id,
        "ran_ms": int(time.time() * 1000),
        "config_overrides": overrides,
        "total": len(results),
        "recovered": len(recovered),
        "still_failing": len(results) - len(recovered),
        "results": results,
    }


def public_diff(diff: dict) -> dict:
    """Strip internal full-event payloads for API responses."""
    out = dict(diff)
    out["results"] = [{k: v for k, v in r.items() if k != "_events"}
                      for r in diff["results"]]
    return out
