"""Verification suite for the log collector.

Covers: multi-line stacks, nested JSON, truncated lines, mixed timezones,
original line numbers, interrupt/resume of unclosed events, dead-letter
quarantine + replay, duplicate reads, file rotation, cross-batch stitching.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logcollector.assembler import AssemblerConfig, EventAssembler, RawLine
from logcollector.collector import Config, LogCollector
from logcollector.replay import replay_errors
from logcollector.timestamps import parse_timestamp


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lc-test-")
        self.logs = os.path.join(self.dir, "logs")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.logs)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def logpath(self, name="app.log"):
        return os.path.join(self.logs, name)

    def write(self, text, name="app.log", mode="w"):
        path = self.logpath(name)
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(text)
        return path

    def collector(self, **cfgkw):
        cfg = Config(**cfgkw) if cfgkw else Config()
        return LogCollector([self.logs], self.state, cfg)


class TestAssembler(TmpDir):
    def feed_lines(self, lines, **cfgkw):
        asm = EventAssembler("t.log", 0, AssemblerConfig(**cfgkw))
        out = []
        for i, ln in enumerate(lines, 1):
            out += asm.feed(RawLine("t.log", 0, i, i * 100, ln))
        out += asm.flush("test-eof")  # close the still-open tail event
        return out

    def test_python_traceback_single_event(self):
        out = self.feed_lines([
            "2026-11-18 10:00:00,123 ERROR job failed",
            "Traceback (most recent call last):",
            '  File "worker.py", line 42, in run',
            "    raise ValueError('boom')",
            "ValueError: boom",
            "2026-11-18 10:00:01,000 INFO recovered",
        ])
        events = [p for k, p in out if k == "event"]
        self.assertEqual(len(events), 2)
        ev = events[0]
        self.assertTrue(ev["has_stack"])
        self.assertEqual(ev["level"], "ERROR")
        self.assertEqual(ev["start_line"], 1)
        self.assertEqual(ev["end_line"], 5)
        self.assertEqual(ev["line_count"], 5)
        self.assertEqual([l["line_no"] for l in ev["lines"]], [1, 2, 3, 4, 5])
        self.assertEqual(events[1]["level"], "INFO")

    def test_java_stack_with_caused_by(self):
        out = self.feed_lines([
            "2026-11-18T10:00:00.000Z ERROR http handler blew up",
            "java.lang.IllegalStateException: nope",
            "\tat com.acme.Handler.handle(Handler.java:42)",
            "\tat com.acme.Server.main(Server.java:10)",
            "Caused by: java.io.IOException: disk",
            "\t... 3 more",
        ])
        events = [p for k, p in out if k == "event"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["has_stack"])
        self.assertEqual(events[0]["line_count"], 6)

    def test_nested_pretty_json(self):
        out = self.feed_lines([
            "{",
            '  "timestamp": "2026-11-18T10:00:00Z",',
            '  "level": "warn",',
            '  "message": "brace } inside { string",',
            '  "ctx": {',
            '    "user": {"id": 7, "tags": ["a", "b"]},',
            '    "nested": {"deep": {"deeper": [1, 2, {"x": "y"}]}}',
            "  }",
            "}",
        ])
        events = [p for k, p in out if k == "event"]
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["kind"], "json")
        self.assertEqual(ev["json"]["ctx"]["user"]["id"], 7)
        self.assertEqual(ev["level"], "WARN")
        self.assertEqual(ev["ts_utc"],
                         parse_timestamp("2026-11-18T10:00:00Z")[0])
        self.assertEqual(ev["time_source"], "json")
        self.assertEqual(ev["start_line"], 1)
        self.assertEqual(ev["end_line"], 9)

    def test_single_line_json_and_bracket_level(self):
        out = self.feed_lines([
            '{"ts": 1700000000000, "msg": "one-liner"}',
            "[INFO] plain bracket-level line",
        ])
        events = [p for k, p in out if k == "event"]
        errors = [p for k, p in out if k == "error"]
        self.assertEqual(len(events), 2)
        self.assertEqual(errors, [])
        self.assertEqual(events[0]["kind"], "json")
        self.assertEqual(events[0]["ts_utc"], 1700000000000)
        self.assertEqual(events[1]["kind"], "unstructured")
        self.assertEqual(events[1]["level"], "INFO")

    def test_bad_json_quarantined_following_events_survive(self):
        out = self.feed_lines([
            "2026-11-18T10:00:00Z INFO before",
            "{not json at all}",
            "2026-11-18T10:00:01Z INFO after",
        ])
        events = [p for k, p in out if k == "event"]
        errors = [p for k, p in out if k == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "json-parse")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["message"].endswith("after"), True)

    def test_unclosed_json_interrupted_by_timestamp(self):
        out = self.feed_lines([
            '{"a": 1,',
            '  "b": "still going",',
            "2026-11-18T10:00:00Z INFO next event",
            "2026-11-18T10:00:01Z INFO another",
        ])
        events = [p for k, p in out if k == "event"]
        errors = [p for k, p in out if k == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["reason"], "json-unclosed")
        self.assertEqual([e["message"].split()[-1] for e in events],
                         ["event", "another"])

    def test_mixed_timezones_same_instant(self):
        out = self.feed_lines([
            "2026-11-18T10:00:00Z INFO zulu",
            "2026-11-18T18:00:00+08:00 INFO shanghai",
            "2026-11-18 05:00:00,000 -0500 INFO eastern",
            "2026-11-18 10:00:00,000 INFO naive-utc-default",
        ])
        events = [p for k, p in out if k == "event"]
        self.assertEqual(len(events), 4)
        ts = [e["ts_utc"] for e in events]
        self.assertEqual(len(set(ts)), 1, f"mixed tz not normalised: {ts}")
        self.assertFalse(events[0]["tz_assumed"])
        self.assertFalse(events[1]["tz_assumed"])
        self.assertFalse(events[2]["tz_assumed"])
        self.assertTrue(events[3]["tz_assumed"])

    def test_syslog_timestamp_assumed(self):
        out = self.feed_lines(["Nov 18 10:20:30 host app: hello"])
        ev = [p for k, p in out if k == "event"][0]
        self.assertTrue(ev["tz_assumed"])
        self.assertEqual(ev["level"], None)

    def test_state_roundtrip_mid_event(self):
        cfg = AssemblerConfig()
        asm = EventAssembler("t.log", 0, cfg)
        asm.feed(RawLine("t.log", 0, 1, 0, "2026-11-18T10:00:00Z ERROR boom"))
        asm.feed(RawLine("t.log", 0, 2, 40, "Traceback (most recent call last):"))
        state = asm.dump_state()
        asm2 = EventAssembler.from_state(json.loads(json.dumps(state)), cfg)
        out = asm2.feed(RawLine("t.log", 0, 3, 80, '  File "x.py", line 1'))
        out += asm2.feed(RawLine("t.log", 0, 4, 100, "2026-11-18T10:00:01Z INFO done"))
        events = [p for k, p in out if k == "event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["line_count"], 3)
        self.assertEqual([l["line_no"] for l in events[0]["lines"]], [1, 2, 3])


class TestCollector(TmpDir):
    def test_line_numbers_and_offsets(self):
        self.write("2026-11-18T10:00:00Z INFO first\n"
                   "continuation line\n"
                   "2026-11-18T10:00:01Z INFO second\n")
        col = self.collector()
        col.poll()
        col.flush_all()
        evs = col.events.timeline()
        self.assertEqual(len(evs), 2)
        self.assertEqual((evs[0]["start_line"], evs[0]["end_line"]), (1, 2))
        self.assertEqual((evs[1]["start_line"], evs[1]["end_line"]), (3, 3))
        raw = open(self.logpath(), "rb").read()
        for ev in evs:
            chunk = raw[ev["start_offset"]:ev["end_offset"]]
            self.assertTrue(chunk.startswith(
                ev["message"].split(" INFO ")[0].encode()))

    def test_cross_batch_stitching(self):
        path = self.write('{"a": 1,\n')
        col = self.collector()
        s1 = col.poll()
        self.assertEqual(len(col.events), 0)  # incomplete JSON held open
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(' "b": [1, 2, {"c": 3}]}\n')
        col.poll()
        evs = col.events.timeline()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["json"]["b"], [1, 2, {"c": 3}])
        self.assertEqual((evs[0]["start_line"], evs[0]["end_line"]), (1, 2))

    def test_partial_line_without_newline_stitched(self):
        path = self.write("2026-11-18T10:00:00Z INFO hel")
        col = self.collector()
        col.poll()
        self.assertEqual(len(col.events), 0)  # no newline -> nothing emitted
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("lo world\n")
        col.poll()
        col.flush_all()
        evs = col.events.timeline()
        self.assertEqual(len(evs), 1)
        self.assertIn("hello world", evs[0]["message"])

    def test_truncated_long_line(self):
        long_line = "2026-11-18T10:00:00Z INFO " + "x" * 5000
        self.write(long_line + "\n2026-11-18T10:00:01Z INFO next\n")
        col = self.collector(max_line_bytes=1024)
        col.poll()
        col.flush_all()
        evs = col.events.timeline()
        self.assertEqual(len(evs), 2)
        self.assertTrue(evs[0]["truncated"])
        line_meta = evs[0]["lines"][0]
        self.assertTrue(line_meta["truncated"])
        self.assertEqual(line_meta["orig_len"], len(long_line))
        self.assertEqual(line_meta["byte_len"], len(long_line))
        # the kept text is capped at max_line_bytes; the summary at 300 chars
        self.assertEqual(len(evs[0]["message"]), 300)
        # offsets still line up with the physical file
        self.assertEqual(evs[1]["lines"][0]["offset"],
                         len(long_line) + 1)

    def test_interrupt_resume_continues_unclosed_event(self):
        path = self.write("2026-11-18T10:00:00Z ERROR boom\n"
                          "Traceback (most recent call last):\n")
        col = self.collector()
        col.poll()
        del col  # "crash": checkpoint on disk holds the open event

        with open(path, "a", encoding="utf-8") as fh:
            fh.write('  File "w.py", line 9, in <module>\n'
                     "ValueError: boom\n"
                     "2026-11-18T10:00:05Z INFO later\n")
        col2 = self.collector()
        col2.poll()
        col2.flush_all()
        evs = col2.events.timeline()
        self.assertEqual(len(evs), 2)
        stack = evs[0]
        self.assertTrue(stack["has_stack"])
        self.assertEqual(stack["line_count"], 4)
        self.assertEqual([l["line_no"] for l in stack["lines"]], [1, 2, 3, 4])

    def test_duplicate_reads_idempotent(self):
        self.write("2026-11-18T10:00:00Z INFO one\n"
                   "2026-11-18T10:00:01Z INFO two\n")
        col = self.collector()
        col.poll()
        col.flush_all()
        s2 = col.poll()  # nothing new
        self.assertEqual(s2["files"][self.logpath()]["events"], 0)
        self.assertEqual(len(col.events), 2)
        ids = {e["event_id"] for e in col.events.timeline()}

        # a brand-new collector over the same file reproduces identical ids
        state2 = os.path.join(self.dir, "state2")
        col2 = LogCollector([self.logs], state2, Config())
        col2.poll()
        col2.flush_all()
        ids2 = {e["event_id"] for e in col2.events.timeline()}
        self.assertEqual(ids, ids2)

    def test_rotation_rename(self):
        path = self.write("2026-11-18T10:00:00Z ERROR old gen\n"
                          "Traceback (most recent call last):\n")
        col = self.collector()
        col.poll()
        os.replace(path, path + ".1")          # rotate away
        self.write("2026-11-18T10:01:00Z INFO new gen\n")
        col.poll()
        col.flush_all()
        evs = col.events.timeline()
        self.assertEqual(len(evs), 2)
        old, new = evs
        self.assertEqual(old["generation"], 0)
        self.assertTrue(old["incomplete"])     # open event flushed, not lost
        self.assertEqual(old["flush_reason"], "rotation")
        self.assertEqual(new["generation"], 1)
        self.assertEqual(new["start_line"], 1)  # line numbers restart per gen

    def test_rotation_copytruncate(self):
        path = self.write("2026-11-18T10:00:00Z INFO before\n")
        col = self.collector()
        col.poll()
        with open(path, "w", encoding="utf-8") as fh:  # same inode, shrinks
            fh.write("2026-11-18T10:02:00Z INFO after\n")
        col.poll()
        col.flush_all()
        evs = col.events.timeline()
        self.assertEqual(len(evs), 2)
        self.assertEqual({e["generation"] for e in evs}, {0, 1})
        self.assertEqual(evs[1]["message"].split()[-1], "after")

    def test_bad_record_does_not_lose_followers(self):
        self.write("2026-11-18T10:00:00Z INFO ok-1\n"
                   "{broken json\n"
                   "2026-11-18T10:00:01Z INFO ok-2\n"
                   "2026-11-18T10:00:02Z INFO ok-3\n")
        col = self.collector()
        col.poll()
        col.flush_all()
        evs = col.events.timeline()
        errs = col.errors.unresolved()
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["reason"], "json-unclosed")
        self.assertEqual([e["message"].split()[-1] for e in evs],
                         ["ok-1", "ok-2", "ok-3"])

    def test_replay_recovers_and_is_idempotent(self):
        self.write('{"a": 1, "b": 2,}\n'          # trailing comma -> strict fails
                   "{totally not json}\n"            # genuinely broken
                   "2026-11-18T10:00:00Z INFO tail\n")
        col = self.collector()
        col.poll()
        col.flush_all()
        self.assertEqual(len(col.errors.unresolved()), 2)
        self.assertEqual(len(col.events), 1)

        diff = replay_errors(col.errors.unresolved(),
                             col.cfg.assembler_config(),
                             {"json_strict": False})
        self.assertEqual(diff["total"], 2)
        self.assertEqual(diff["recovered"], 1)
        self.assertEqual(diff["still_failing"], 1)
        rec = [r for r in diff["results"] if r["status"] == "recovered"][0]
        self.assertTrue(rec["recovered_events"][0]["json_salvaged"])

        # apply: event lands in store with its *original* id, error resolved
        for res in diff["results"]:
            if res["status"] == "recovered":
                for ev in res["_events"]:
                    col.events.add(ev)
                col.errors.mark_resolved([res["error_id"]], diff["replay_id"])
        self.assertEqual(len(col.events), 2)
        self.assertEqual(len(col.errors.unresolved()), 1)

        # replaying again only sees the still-broken one; re-adding is a no-op
        diff2 = replay_errors(col.errors.unresolved(),
                              col.cfg.assembler_config(),
                              {"json_strict": False})
        self.assertEqual(diff2["total"], 1)
        self.assertEqual(diff2["recovered"], 0)
        n = col.events.add_many(
            ev for r in diff["results"] for ev in r["_events"])
        self.assertEqual(n, 0)

    def test_max_event_lines_overflow(self):
        lines = ["2026-11-18T10:00:00Z ERROR start"] + ["\tat x.y(z:1)"] * 300
        self.write("\n".join(lines) + "\n")
        col = self.collector(max_event_lines=50)
        col.poll()
        evs = col.events.timeline()
        self.assertTrue(any(e.get("overflow") for e in evs))
        # nothing dropped: overflow chunks + remainder cover all 301 lines
        covered = sum(e["line_count"] for e in evs)
        self.assertEqual(covered, 301)


class TestServer(TmpDir):
    def test_api_endpoints(self):
        import threading
        import urllib.request
        from logcollector.server import serve

        self.write("2026-11-18T10:00:00Z INFO hello\n{bad json}\n")
        col = self.collector()
        col.poll()
        server = serve(col, "127.0.0.1", 0, poll_interval=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return json.loads(r.read().decode())

        status = get("/api/status")
        self.assertEqual(status["events_total"], 1)
        self.assertEqual(status["errors_unresolved"], 1)
        self.assertEqual(status["files"][0]["lines_read"], 2)

        events = get("/api/events")
        self.assertEqual(len(events["events"]), 1)

        errors = get("/api/errors")
        self.assertEqual(errors["errors"][0]["reason"], "json-parse")

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/replay",
            data=json.dumps({"apply": False,
                             "overrides": {"json_strict": False}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            rep = json.loads(r.read().decode())
        self.assertEqual(rep["replay"]["total"], 1)
        self.assertEqual(rep["replay"]["still_failing"], 1)

        with open(self.logpath(), "rb") as fh:  # dashboard is served
            pass
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("logcollector", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
