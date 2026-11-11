#!/usr/bin/env python3
"""Drive a real steerd through `attach`, the way the app does.

Spends a few cents of Claude/Codex usage with the smallest models. Usage:

    python3 helper/tests/live_check.py <project-dir> [claude|codex|resume|misc ...]

Runs against an isolated STEER_HOME (~/.local/share/steer-test) and a daemon
started without launchd, so it never touches the real helper.
"""
import json
import os
import subprocess
import sys
import threading
import queue
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STEERD = os.path.join(HERE, "..", "steerd.py")
ENV = dict(os.environ,
           STEER_HOME=os.path.expanduser("~/.local/share/steer-test"),
           STEER_NO_LAUNCHD="1")


class Conn:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, STEERD, "attach"], env=ENV, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, bufsize=0)
        self.q = queue.Queue()
        self.events = queue.Queue()
        self.next = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            m = json.loads(line)
            (self.q if "id" in m else self.events).put(m)

    def call(self, op, timeout=180, **kw):
        self.next += 1
        req = dict(kw, id=self.next, op=op)
        self.p.stdin.write((json.dumps(req) + "\n").encode())
        end = time.time() + timeout
        while time.time() < end:
            m = self.q.get(timeout=timeout)
            if m["id"] == self.next:
                if not m["ok"]:
                    raise RuntimeError("%s failed: %s" % (op, m["error"]))
                return m["result"]
        raise TimeoutError(op)

    def wait(self, pred, timeout=180, on_event=None):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev = self.events.get(timeout=1)
            except queue.Empty:
                continue
            if on_event:
                on_event(ev)
            if pred(ev):
                return ev
        raise TimeoutError("event")

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def show(ev):
    kind = ev.get("ev")
    if kind == "item":
        it = ev["item"]
        print("  item", it["kind"], it.get("status", ""), it.get("category", ""),
              (it.get("text") or it.get("target") or "")[:80].replace("\n", " "), "+%s-%s" % (it.get("add"), it.get("del")) if it.get("diff") else "")
    elif kind == "request":
        r = ev["request"]
        print("  REQUEST", r["kind"], r.get("category", ""), r.get("target", "")[:80], [q["question"] for q in r.get("questions", [])])
    elif kind == "resolved":
        print("  resolved", ev["rid"])
    elif kind == "session":
        s = ev["session"]
        print("  session", s["state"], s.get("needs"), s.get("mode"), (s.get("lastText") or "")[:50])


def run_turn(c, sid, decide=None, timeout=240):
    """Wait for the turn to end, answering requests with decide(request)."""
    def handle(ev):
        show(ev)
        if ev.get("ev") == "request" and ev.get("sid") == sid:
            decision, extra = decide(ev["request"]) if decide else ("allow", {})
            c.call("session.respond", sid=sid, rid=ev["request"]["rid"], decision=decision, **extra)
    c.wait(lambda ev: ev.get("ev") == "item" and ev["item"]["kind"] == "turn" and ev.get("sid") == sid, timeout, handle)


def main():
    proj = os.path.abspath(sys.argv[1])
    steps = sys.argv[2:] or ["misc", "claude", "codex"]
    c = Conn()
    hello = c.call("hello")
    print("hello", hello["helper"], hello["agents"])
    state_file = os.path.join(ENV["STEER_HOME"], "live-state.json")
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}

    if "misc" in steps:
        lst = c.call("sessions.list")["sessions"]
        print("sessions", len(lst), [(s["agent"], s["source"], s["title"][:30]) for s in lst[:4]])
        print("projects", [p["name"] for p in c.call("projects.recent")["projects"][:6]])
        print("files", c.call("files.search", cwd=proj, query="calc")["files"])
        print("dirs", [e["name"] for e in c.call("dirs.list", path="~")["entries"][:6]])
        info = c.call("agent.info", agent="codex")
        print("codex models", [m["id"] for m in info["models"]][:4], info["modes"])

    if "claude" in steps:
        r = c.call("session.create", agent="claude", cwd=proj, model="haiku",
                   text="Run `ls`, then add a function div(a, b) to calc.py. Keep it minimal. One sentence reply.")
        sid = r["session"]["sid"]
        state["claude"] = sid
        print("claude session", sid)
        run_turn(c, sid, decide=lambda req: ("allowSession", {}))
        c.call("session.send", sid=sid, text="Use the AskUserQuestion tool to ask me tabs or spaces, then reply with my choice only.")
        run_turn(c, sid, decide=lambda req: ("allow", {"answers": {req["questions"][0]["id"]: req["questions"][0]["options"][1]["label"]}})
                 if req["kind"] == "question" else ("allow", {}))
        snap = c.call("session.open", sid=sid)
        print("snapshot", len(snap["items"]), [i["kind"] for i in snap["items"]], snap["session"]["state"])

    if "resume" in steps:
        sid = state["claude"]
        snap = c.call("session.open", sid=sid)
        print("reopened", len(snap["items"]), snap["session"]["title"], [i["kind"] for i in snap["items"]][:12])
        c.call("session.send", sid=sid, text="Reply with exactly: resumed")
        run_turn(c, sid)

    if "codex" in steps:
        r = c.call("session.create", agent="codex", cwd=proj, model="gpt-5.6-luna", mode="untrusted",
                   text="Run `ls` in the shell, then add a function mod(a, b) to calc.py. Keep it minimal. One sentence reply.")
        sid = r["session"]["sid"]
        state["codex"] = sid
        print("codex session", sid)
        run_turn(c, sid, decide=lambda req: ("allowSession", {}))
        snap = c.call("session.open", sid=sid)
        print("snapshot", len(snap["items"]), [i["kind"] for i in snap["items"]], snap["session"]["state"])

    json.dump(state, open(state_file, "w"))
    c.close()


if __name__ == "__main__":
    main()
