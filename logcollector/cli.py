"""Command line interface.

  python -m logcollector serve  --log DIR|GLOB|FILE [--state-dir D] [--port P]
  python -m logcollector poll   --log ... [--state-dir D]      # one-shot
  python -m logcollector status [--state-dir D]
  python -m logcollector flush  [--state-dir D]                # close open events
  python -m logcollector replay [--state-dir D] [--apply] [--no-strict]
"""
from __future__ import annotations

import argparse
import json
import sys

from .collector import Config, LogCollector
from .replay import public_diff, replay_errors


def _collector(args) -> LogCollector:
    cfg = Config(
        max_line_bytes=args.max_line_bytes,
        json_strict=not getattr(args, "no_strict", False),
        default_tz_name=("offset" if args.tz_offset is not None else "UTC"),
        default_tz_offset_min=args.tz_offset or 0,
    )
    return LogCollector(args.log or [], args.state_dir, cfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="logcollector")
    ap.add_argument("--state-dir", default=".lcstate")
    ap.add_argument("--log", action="append",
                    help="file, directory (*.log) or glob; repeatable")
    ap.add_argument("--max-line-bytes", type=int, default=16 * 1024)
    ap.add_argument("--tz-offset", type=int, default=None,
                    help="default timezone for naive timestamps, in minutes "
                         "east of UTC (e.g. 480 for UTC+8)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8410)
    p.add_argument("--interval", type=float, default=1.0,
                   help="poll interval seconds; 0 disables background polling")

    sub.add_parser("poll")
    sub.add_parser("status")
    sub.add_parser("flush")

    p = sub.add_parser("replay")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--no-strict", action="store_true",
                   help="allow JSON salvage (trailing commas) during replay")
    p.add_argument("--max-json-lines", type=int, default=None)

    args = ap.parse_args(argv)
    col = _collector(args)

    if args.cmd == "serve":
        from .server import serve
        server = serve(col, args.host, args.port, args.interval)
        print(f"logcollector serving http://{args.host}:{args.port} "
              f"(state: {args.state_dir})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            col.save_checkpoint()
        return 0

    if args.cmd == "poll":
        print(json.dumps(col.poll(), ensure_ascii=False, indent=1))
    elif args.cmd == "status":
        print(json.dumps(col.status(), ensure_ascii=False, indent=1))
    elif args.cmd == "flush":
        print(json.dumps(col.flush_all(), ensure_ascii=False))
    elif args.cmd == "replay":
        overrides = {"json_strict": False} if args.no_strict else {}
        if args.max_json_lines:
            overrides["max_json_lines"] = args.max_json_lines
        diff = replay_errors(col.errors.unresolved(),
                             col.cfg.assembler_config(), overrides)
        if args.apply:
            ids = []
            for res in diff["results"]:
                if res["status"] == "recovered":
                    for ev in res["_events"]:
                        col.events.add(ev)
                    ids.append(res["error_id"])
            col.errors.mark_resolved(ids, diff["replay_id"])
        print(json.dumps(public_diff(diff), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
