#!/usr/bin/env python3
"""Generate messy demo logs in stages, to exercise the collector live.

  python3 scripts/make_demo_logs.py --dir demo/logs --stage 1   # initial batch
  python3 scripts/make_demo_logs.py --dir demo/logs --stage 2   # append (cross-batch JSON, bad records, long line)
  python3 scripts/make_demo_logs.py --dir demo/logs --stage 3   # rotate app.log, new generation, unclosed tail

Stage 1 ends *inside* a pretty-printed JSON object so the event spans two
poll batches; stage 3 rotates the file (new inode) and leaves an unclosed
exception at the tail, visible as an open event in the dashboard.
"""
import argparse
import os
import shutil

LONG_LINE = "2026-11-18T10:20:31Z INFO payload=" + "x" * 20000  # > 16KiB -> truncated

STAGE1_APP = """\
2026-11-18T10:20:30.123Z INFO  server boot pid=4211
2026-11-18T18:20:30.456+08:00 INFO  shanghai gateway online
2026-11-18 05:20:30,789 -0500 WARN  eastern replica lag 120ms
2026-11-18 10:20:31,001 INFO  naive local timestamp (default tz)
Nov 18 10:20:31 app[4211]: syslog style line, no year
{"ts": "2026-11-18T10:20:32Z", "level": "info", "msg": "single-line json", "meta": {"a": 1}}
2026-11-18T10:20:33.000Z ERROR worker crashed
Traceback (most recent call last):
  File "worker.py", line 42, in run
    raise ValueError("boom")
ValueError: boom
2026-11-18T10:20:34.000Z ERROR java bridge failed
java.lang.IllegalStateException: queue full
\\tat com.acme.Queue.push(Queue.java:87)
\\tat com.acme.Bridge.main(Bridge.java:12)
Caused by: java.io.IOException: reset by peer
\\t... 3 more
{
  "timestamp": "2026-11-18T10:20:35+08:00",
  "level": "warn",
  "message": "pretty json split across batches",
  "ctx": {
    "user": {"id": 7, "tags": ["vip", "beta"]},
    "note": "braces } and { inside strings",
    "retry": {"attempt": 3, "backoff": [1, 2, 4]}
"""

STAGE2_APP = """\
  }
}
2026-11-18T10:20:36.000Z INFO json batch complete
{"a": 1, "b": 2,}
{this is not json at all}
{"unclosed": "object starts here",
  "more": "but writer died",
2026-11-18T10:20:37.000Z INFO after the broken object
""" + LONG_LINE + """
2026-11-18T10:20:38.000Z INFO tail of stage 2
"""

STAGE1_OTHER = """\
18/Nov/2026:10:20:39 +0000 apache-style line with offset
Nov 18 10:20:40 app[99]: another syslog line
plain orphan line without any timestamp
"""

STAGE3_APP = """\
2026-11-18T10:21:00.000Z INFO new generation after rotation
2026-11-18T10:21:01.000Z ERROR fresh crash on gen 1
Traceback (most recent call last):
  File "worker.py", line 77, in run
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="demo/logs")
    ap.add_argument("--stage", type=int, required=True, choices=[1, 2, 3])
    args = ap.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    app = os.path.join(args.dir, "app.log")
    other = os.path.join(args.dir, "other.log")

    if args.stage == 1:
        if os.path.exists(args.dir):
            shutil.rmtree(args.dir)
        os.makedirs(args.dir)
        with open(app, "w", encoding="utf-8") as fh:
            fh.write(STAGE1_APP)
        with open(other, "w", encoding="utf-8") as fh:
            fh.write(STAGE1_OTHER)
        print(f"stage 1: wrote {app} (ends mid-JSON) and {other}")
    elif args.stage == 2:
        with open(app, "a", encoding="utf-8") as fh:
            fh.write(STAGE2_APP)
        print(f"stage 2: appended to {app} (JSON completes; bad records; 20KB line)")
    else:
        os.replace(app, app + ".1")
        with open(app, "w", encoding="utf-8") as fh:
            fh.write(STAGE3_APP)
        print(f"stage 3: rotated {app} -> {app}.1, new generation "
              f"(ends with an unclosed traceback)")


if __name__ == "__main__":
    main()
