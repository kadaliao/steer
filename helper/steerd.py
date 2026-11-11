#!/usr/bin/env python3
"""Steer host helper.

Keeps Claude Code and Codex sessions running on this machine for the Steer
iPhone app, which reaches it over SSH (`steerd.py attach`). The helper turns
both agents' structured protocols into one small timeline model, remembers it
while the phone is away, and hands the app a snapshot when it comes back.

Single file, standard library only, Python 3.8+.

Commands:
  attach    connect stdin/stdout to the daemon, starting it if needed
  serve     run the daemon in the foreground
  stop      ask a running daemon to exit
  version   print the helper version
  doctor    print what the helper can see (agents, daemon, paths)
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import glob
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

VERSION = "0.4.2"
PROTO = 1

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("STEER_HOME") or os.path.join(HOME, ".local", "share", "steer")
SOCK = os.environ.get("STEER_SOCK") or os.path.join(DATA_DIR, "steerd.sock")
LOG_PATH = os.path.join(DATA_DIR, "steerd.log")
META_DIR = os.path.join(DATA_DIR, "sessions")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CACHE_PATH = os.path.join(DATA_DIR, "agent-info.json")
PUSH_DEVICES = os.path.join(DATA_DIR, "push-devices.json")
APNS_DIR = os.path.join(DATA_DIR, "apns")
LABEL = "com.kadaliao.steer.helper"
LINE_LIMIT = 256 * 1024 * 1024

OUTPUT_TAIL = 4000
DIFF_LIMIT = 40000
SNAPSHOT_ITEMS = 400
CLAUDE_IDLE_EXIT = 30 * 60
# After a phone turn on a session taken over from a herdr/tmux pane, how long
# to wait (idle) before reopening it in that pane.
HANDBACK_DELAY = int(os.environ.get("STEER_HANDBACK_DELAY", "60"))
TAKEOVER_WAIT = 30 * 60
HISTORY_DAYS = 30
HISTORY_LIMIT = 80

EXTRA_BIN_DIRS = [
    os.path.join(HOME, ".local", "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.join(HOME, ".npm-global", "bin"),
    os.path.join(HOME, ".bun", "bin"),
    os.path.join(HOME, ".volta", "bin"),
    os.path.join(HOME, ".cargo", "bin"),
]


def now_ms() -> int:
    return int(time.time() * 1000)


def log(*parts) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, file=sys.stderr, flush=True)


def tail(text: str, limit: int) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else "…" + text[-limit:]


def head(text: str, limit: int) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def one_line(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return head(text, limit)


def find_bin(name: str):
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def rel_path(path: str, cwd: str) -> str:
    if not path:
        return ""
    try:
        if cwd and (path == cwd or path.startswith(cwd.rstrip("/") + "/")):
            return os.path.relpath(path, cwd)
    except ValueError:
        pass
    if path.startswith(HOME + "/"):
        return "~/" + path[len(HOME) + 1:]
    return path


def diff_counts(diff: str):
    add = rem = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            rem += 1
    return add, rem


def unified(old: str, new: str, label: str) -> str:
    lines = difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile="a/" + label, tofile="b/" + label, lineterm="", n=2)
    return "\n".join(lines)


def patch_from_hunks(hunks, label: str) -> str:
    out = ["--- a/" + label, "+++ b/" + label]
    for h in hunks or []:
        out.append("@@ -%s,%s +%s,%s @@" % (h.get("oldStart"), h.get("oldLines"), h.get("newStart"), h.get("newLines")))
        out.extend(h.get("lines") or [])
    return "\n".join(out)


def text_of(content) -> str:
    """Flatten Claude tool_result / message content to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Claude: tool presentation and transcript parsing
# ---------------------------------------------------------------------------

def claude_tool_view(name: str, inp: dict, cwd: str) -> dict:
    """Describe a Claude tool call as category + target (+ diff)."""
    inp = inp or {}
    view = {"tool": name, "category": "other", "target": "", "detail": ""}
    if name == "Bash":
        view.update(category="command", target=inp.get("command", ""), detail=inp.get("description", ""))
    elif name in ("Read", "NotebookRead"):
        view.update(category="read", target=rel_path(inp.get("file_path") or inp.get("notebook_path", ""), cwd))
    elif name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path", "")
        label = rel_path(path, cwd)
        view.update(category="edit", target=label)
        if name == "Edit":
            diff = unified(inp.get("old_string", ""), inp.get("new_string", ""), label)
        elif name == "MultiEdit":
            diff = "\n".join(unified(e.get("old_string", ""), e.get("new_string", ""), label) for e in inp.get("edits") or [])
        elif name == "Write":
            diff = unified("", inp.get("content", ""), label)
        else:
            diff = ""
        if diff:
            view["diff"] = head(diff, DIFF_LIMIT)
            view["add"], view["del"] = diff_counts(diff)
    elif name in ("Grep", "Glob"):
        target = inp.get("pattern", "")
        if inp.get("path"):
            target += "  " + rel_path(inp.get("path"), cwd)
        view.update(category="search", target=target)
    elif name in ("WebFetch", "WebSearch"):
        view.update(category="web", target=inp.get("url") or inp.get("query", ""))
    elif name in ("Task", "Agent"):
        view.update(category="agent", target=inp.get("description", ""), detail=inp.get("prompt", "")[:400])
    elif name == "TodoWrite":
        view.update(category="todo", target="")
    elif name == "Skill":
        view.update(category="skill", target=inp.get("skill") or inp.get("command", ""))
    elif name in ("ToolSearch", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "ListMcpResourcesTool"):
        view.update(category="meta", target="")
    elif name.startswith("mcp__"):
        parts = name.split("__")
        view.update(category="mcp", target=" · ".join(p for p in parts[1:] if p))
    else:
        view.update(category="other", target=one_line(json.dumps(inp, ensure_ascii=False), 120))
    return view


def todo_plan(inp: dict):
    steps = []
    for t in (inp or {}).get("todos") or []:
        status = t.get("status")
        steps.append({
            "step": t.get("content") or t.get("activeForm") or "",
            "status": {"in_progress": "inProgress", "completed": "completed"}.get(status, "pending"),
        })
    return steps


def is_noise_user_text(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    for marker in ("<local-command-caveat>", "<local-command-stdout>", "<system-reminder>", "<task-notification>"):
        if stripped.startswith(marker):
            return True
    return False


def clean_user_text(text: str) -> str:
    text = text or ""
    if text.startswith("<command-name>"):
        # "/compact" style invocations: show the command line itself.
        name = text.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
        args = ""
        if "<command-args>" in text:
            args = text.split("<command-args>", 1)[1].split("</command-args>", 1)[0]
        return (name + " " + args).strip()
    return text


class ClaudeTranscript:
    """Build timeline items from Claude's own jsonl transcript."""

    def __init__(self, cwd: str, listener=None):
        self.cwd = cwd
        self.items = []
        self.index = {}
        self.title = None
        self.text_counts = {}
        self.listener = listener  # called with each new or changed item

    def put(self, item):
        if item["id"] in self.index:
            current = self.items[self.index[item["id"]]]
            current.update(item)
            item = current
        else:
            self.index[item["id"]] = len(self.items)
            self.items.append(item)
        if self.listener:
            self.listener(item)

    def emit_item(self, item):
        if self.listener:
            self.listener(item)

    def get(self, item_id):
        pos = self.index.get(item_id)
        return self.items[pos] if pos is not None else None

    def feed(self, m: dict):
        t = m.get("type")
        if t in ("ai-title", "custom-title"):
            self.title = m.get("customTitle") or m.get("aiTitle") or self.title
            return
        if m.get("isSidechain"):
            return
        ts = parse_ts(m.get("timestamp"))
        if t == "user":
            msg = m.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                result = m.get("toolUseResult", m.get("tool_use_result"))
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        apply_tool_result(self, b, result, self.cwd, ts)
                return
            if m.get("isMeta") or m.get("isCompactSummary"):
                return
            text = clean_user_text(text_of(content))
            if is_noise_user_text(text):
                return
            if text.startswith("[Request interrupted"):
                self.put({"id": m.get("uuid") or new_id("n"), "kind": "notice", "code": "interrupted", "text": "", "ts": ts})
                return
            images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image") if isinstance(content, list) else 0
            self.put({"id": m.get("uuid") or new_id("u"), "kind": "user", "text": text, "images": images, "ts": ts})
        elif t == "assistant":
            msg = m.get("message") or {}
            msg_id = msg.get("id") or m.get("uuid") or new_id("m")
            for block in msg.get("content") or []:
                add_assistant_block(self, msg_id, block, self.cwd, ts, final=True)
        elif t == "system" and m.get("subtype") == "compact_boundary":
            self.put({"id": m.get("uuid") or new_id("n"), "kind": "notice", "code": "compacted", "text": "", "ts": ts})


def parse_ts(value):
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000)
    except Exception:
        return None


def new_id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:12]


def add_assistant_block(target, msg_id, block, cwd, ts, final):
    """Shared by live stream and transcript: one assistant content block."""
    btype = block.get("type")
    if btype == "text":
        n = target.text_counts.get(msg_id, 0)
        target.text_counts[msg_id] = n + 1
        item_id = "%s:t%d" % (msg_id, n)
        target.put({"id": item_id, "kind": "assistant", "text": block.get("text") or "", "streaming": False, "ts": ts})
        return item_id
    if btype == "tool_use":
        name = block.get("name") or "tool"
        inp = block.get("input") or {}
        if name == "TodoWrite":
            target.put({"id": block.get("id"), "kind": "plan", "steps": todo_plan(inp), "ts": ts})
            return block.get("id")
        if name == "ExitPlanMode":
            target.put({"id": block.get("id"), "kind": "plan", "text": inp.get("plan", ""), "steps": [], "ts": ts})
            return block.get("id")
        view = claude_tool_view(name, inp, cwd)
        item = {"id": block.get("id"), "kind": "tool", "status": "running", "ts": ts}
        item.update(view)
        existing = target.get(block.get("id"))
        if existing and existing.get("status") not in (None, "running"):
            item["status"] = existing["status"]
        target.put(item)
        return block.get("id")
    return None


def apply_tool_result(target, block, result, cwd, ts):
    tool_id = block.get("tool_use_id")
    item = target.get(tool_id)
    if item is None or item.get("kind") != "tool":
        return
    is_error = bool(block.get("is_error"))
    output = text_of(block.get("content"))
    update = {"status": "error" if is_error else "ok", "output": tail(output, OUTPUT_TAIL), "endTs": ts or now_ms()}
    update.pop("pending", None)
    if isinstance(result, dict) and item.get("category") == "edit":
        hunks = result.get("structuredPatch")
        diff = ""
        if hunks:
            diff = patch_from_hunks(hunks, item.get("target", ""))
        elif result.get("type") == "create":
            diff = unified("", result.get("content", ""), item.get("target", ""))
        if diff:
            update["diff"] = head(diff, DIFF_LIMIT)
            update["add"], update["del"] = diff_counts(diff)
    lowered = output.lower()
    if is_error and ("declined" in lowered or "denied" in lowered or "doesn't want to proceed" in lowered):
        update["status"] = "declined"
    item.update(update)
    item.pop("pending", None)
    if hasattr(target, "emit_item"):
        target.emit_item(item)


def read_head_tail(path: str, head_bytes=96 * 1024, tail_bytes=384 * 1024):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        first = f.read(head_bytes)
        if size > head_bytes + tail_bytes:
            f.seek(size - tail_bytes)
            last = f.read()
            last = last[last.find(b"\n") + 1:]
        else:
            last = f.read()
    return first, last


def iter_json_lines(data: bytes):
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except Exception:
            continue


_claude_index_cache = {}


def claude_history_entry(path: str):
    """Summarise one Claude transcript cheaply (head + tail)."""
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    cached = _claude_index_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    first, last = read_head_tail(path)
    sid = os.path.splitext(os.path.basename(path))[0]
    cwd = None
    first_prompt = None
    title = None
    last_text = None
    last_prompt = None
    for m in iter_json_lines(first):
        if cwd is None and m.get("cwd"):
            cwd = m.get("cwd")
        if m.get("type") in ("ai-title", "custom-title"):
            title = m.get("customTitle") or m.get("aiTitle") or title
        if first_prompt is None and m.get("type") == "user" and not m.get("isMeta") and not m.get("isSidechain"):
            content = (m.get("message") or {}).get("content")
            if isinstance(content, str) or (isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
                text = clean_user_text(text_of(content))
                if not is_noise_user_text(text):
                    first_prompt = text
    custom = None
    for m in iter_json_lines(last):
        t = m.get("type")
        if cwd is None and m.get("cwd"):
            cwd = m.get("cwd")
        if t == "ai-title":
            title = m.get("aiTitle") or title
        elif t == "custom-title":
            custom = m.get("customTitle") or custom
        elif t == "last-prompt":
            last_prompt = m.get("lastPrompt") or last_prompt
        elif t == "assistant" and not m.get("isSidechain"):
            for b in (m.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                    last_text = b.get("text")
    if first_prompt is None and last_prompt:
        first_prompt = last_prompt
    if not cwd or (first_prompt is None and last_text is None):
        entry = None
    else:
        entry = {
            "sid": sid,
            "agent": "claude",
            "cwd": cwd,
            "title": custom or title or one_line(first_prompt or "", 60),
            "lastText": one_line(last_text or "", 160),
            "updatedAt": int(st.st_mtime * 1000),
            "path": path,
        }
    _claude_index_cache[path] = (key, entry)
    return entry


def claude_history(limit=HISTORY_LIMIT):
    root = os.path.join(HOME, ".claude", "projects")
    cutoff = time.time() - HISTORY_DAYS * 86400
    files = []
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= cutoff:
            files.append((mtime, path))
    files.sort(reverse=True)
    out = []
    for _, path in files:
        try:
            entry = claude_history_entry(path)
        except Exception as e:  # unreadable transcript: skip it
            log("history: skip", path, e)
            continue
        if entry:
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def claude_transcript_path(native_id: str, cwd: str = None):
    root = os.path.join(HOME, ".claude", "projects")
    if cwd:
        encoded = "".join(ch if ch.isalnum() else "-" for ch in cwd)
        candidate = os.path.join(root, encoded, native_id + ".jsonl")
        if os.path.exists(candidate):
            return candidate
    matches = glob.glob(os.path.join(root, "*", native_id + ".jsonl"))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Codex: item normalisation
# ---------------------------------------------------------------------------

def strip_shell_wrapper(command: str) -> str:
    for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc ", "/bin/sh -c ", "sh -c "):
        if command.startswith(prefix):
            inner = command[len(prefix):].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                inner = inner[1:-1]
                if command[len(prefix)] == '"':
                    inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            return inner
    return command


def codex_item(item: dict, cwd: str, client_ids: dict):
    """Normalise one Codex ThreadItem; returns None for items we hide."""
    t = item.get("type")
    iid = item.get("id")
    ts = item.get("startedAtMs")
    if t == "userMessage":
        text_parts = []
        images = 0
        for c in item.get("content") or []:
            if c.get("type") == "text":
                text_parts.append(c.get("text") or "")
            elif c.get("type") in ("image", "localImage"):
                images += 1
        own = item.get("clientId")
        return {"id": own or iid, "kind": "user", "text": "\n".join(text_parts), "images": images, "ts": ts}
    if t == "agentMessage":
        return {"id": iid, "kind": "assistant", "text": item.get("text") or "", "phase": item.get("phase"),
                "streaming": item.get("completedAtMs") is None, "ts": ts}
    if t == "plan":
        return {"id": iid, "kind": "plan", "text": item.get("text") or "", "steps": [], "ts": ts}
    if t == "commandExecution":
        command = strip_shell_wrapper(item.get("command") or "")
        actions = item.get("commandActions") or []
        category = "command"
        target = command
        if actions and all(a.get("type") in ("read", "listFiles", "search") for a in actions):
            kinds = {a.get("type") for a in actions}
            category = "read" if kinds <= {"read"} else ("search" if "search" in kinds else "read")
            target = ", ".join(rel_path(a.get("path") or "", cwd) or a.get("command", "") for a in actions)
        status = item.get("status")
        exit_code = item.get("exitCode")
        mapped = {"inProgress": "running", "declined": "declined", "failed": "error"}.get(status)
        if mapped is None:
            mapped = "ok" if (exit_code in (None, 0)) else "error"
        out = {"id": iid, "kind": "tool", "tool": "shell", "category": category, "target": target,
               "command": command, "status": mapped, "ts": ts, "endTs": item.get("completedAtMs"),
               "exitCode": exit_code}
        if item.get("aggregatedOutput") is not None:
            out["output"] = tail(item.get("aggregatedOutput") or "", OUTPUT_TAIL)
        return out
    if t == "fileChange":
        diffs = []
        targets = []
        for ch in item.get("changes") or []:
            label = rel_path(ch.get("path") or "", cwd)
            targets.append(label)
            kind = (ch.get("kind") or {}).get("type")
            body = ch.get("diff") or ""
            if kind == "add" and not body.startswith("@@") and body:
                body = "\n".join("+" + line for line in body.splitlines())
            diffs.append("--- a/%s\n+++ b/%s\n%s" % (label, label, body))
        diff = "\n".join(diffs)
        add, rem = diff_counts(diff)
        status = {"inProgress": "running", "completed": "ok", "failed": "error", "declined": "declined"}.get(item.get("status"), "ok")
        return {"id": iid, "kind": "tool", "tool": "apply_patch", "category": "edit", "target": ", ".join(targets),
                "diff": head(diff, DIFF_LIMIT), "add": add, "del": rem, "status": status, "ts": ts,
                "endTs": item.get("completedAtMs")}
    if t == "mcpToolCall":
        status = {"inProgress": "running", "completed": "ok", "failed": "error"}.get(item.get("status"), "ok")
        return {"id": iid, "kind": "tool", "tool": "mcp", "category": "mcp",
                "target": "%s · %s" % (item.get("server"), item.get("tool")), "status": status, "ts": ts}
    if t == "webSearch":
        return {"id": iid, "kind": "tool", "tool": "web_search", "category": "web", "target": item.get("query") or "",
                "status": "ok", "ts": ts}
    if t == "contextCompaction":
        return {"id": iid, "kind": "notice", "code": "compacted", "text": "", "ts": ts}
    if t in ("reasoning", "hookPrompt", "functionCallOutput", "sleep", "subAgentActivity"):
        return None
    if t in ("collabAgentToolCall", "dynamicToolCall", "imageView", "imageGeneration"):
        return {"id": iid, "kind": "tool", "tool": t, "category": "agent" if t == "collabAgentToolCall" else "other",
                "target": item.get("tool") or item.get("path") or item.get("prompt") or "", "status": "ok", "ts": ts}
    return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

COMMANDS_TTL = 600
_open_cache = [0.0, {}]
TERMINAL_IDLE = ("idle", "done", "")


def claude_open_sessions(root=None, max_age=1.0):
    """sessionId -> {pid, status, cwd} for each Claude session a terminal has
    open right now, from the registry Claude Code keeps in
    ~/.claude/sessions/<pid>.json (`entrypoint: cli`; print-mode processes,
    Steer's own included, say `sdk-cli`)."""
    now = time.time()
    if root is None and now - _open_cache[0] < max_age:
        return _open_cache[1]
    found = {}
    for path in glob.glob(os.path.join(root or os.path.join(HOME, ".claude", "sessions"), "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
            pid = int(d.get("pid") or 0)
            if d.get("kind") != "interactive" or str(d.get("entrypoint") or "").startswith("sdk") \
                    or not pid or not d.get("sessionId"):
                continue
            os.kill(pid, 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        found[d["sessionId"]] = {"pid": pid, "status": str(d.get("status") or ""), "cwd": d.get("cwd") or ""}
    if root is None:
        _open_cache[:] = [now, found]
    return found


def pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


async def wait_exit(pid, timeout) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        await asyncio.sleep(0.2)
    return not pid_alive(pid)


TERMINAL_ENV_KEYS = ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_BIN_PATH", "TMUX", "TMUX_PANE")


def process_env(pid) -> dict:
    """The terminal-multiplexer variables in another process's environment."""
    env = {}
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/%d/environ" % pid, "rb") as f:
                pairs = [p.decode("utf-8", "replace") for p in f.read().split(b"\0")]
        else:
            out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5).stdout.decode("utf-8", "replace")
            pairs = out.split()
    except (OSError, subprocess.SubprocessError):
        return env
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key in TERMINAL_ENV_KEYS and value:
            env[key] = value
    return env


def process_argv(pid) -> list:
    try:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    return out.split()


_terminal_cache = {}


def _run_sync(args, env=None, timeout=5):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                              timeout=timeout).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def process_ancestors(pid) -> list:
    """Parent, grandparent, … of a process, nearest first."""
    parent = {}
    for line in _run_sync(["ps", "-A", "-o", "pid=,ppid="]).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parent[int(parts[0])] = int(parts[1])
    chain, cur = [], pid
    while cur in parent and parent[cur] not in chain and parent[cur] > 1:
        cur = parent[cur]
        chain.append(cur)
    return chain


def process_stat(pid):
    """(process group, terminal's foreground process group, tty name) of a process."""
    parts = _run_sync(["ps", "-o", "pgid=,tpgid=,tty=", "-p", str(pid)]).split()
    if len(parts) < 3 or not parts[0].isdigit():
        return None, None, None
    return int(parts[0]), int(parts[1]) if parts[1].lstrip("-").isdigit() else None, parts[2]


def pane_shell_if_foreground(term, pgid, tpgid, tty):
    """The pane's shell pid when the process is the pane's foreground job;
    None when it is not (or the pane cannot be asked)."""
    if term["via"] == "herdr":
        out = _run_sync([term["bin"], "pane", "process-info", "--pane", term["pane"]], env=_term_env(term))
        try:
            info = json.loads(out)["result"]["process_info"]
            if int(info["foreground_process_group_id"]) == pgid:
                return int(info["shell_pid"])
        except (ValueError, KeyError, TypeError):
            pass
        return None
    out = _run_sync([term["bin"], "-S", term["socket"], "display-message", "-p", "-t", term["pane"],
                     "#{pane_pid} #{pane_tty}"]).split()
    if len(out) == 2 and out[0].isdigit() and tty and out[1].endswith("/" + tty) and pgid == tpgid:
        return int(out[0])
    return None


def terminal_of(pid) -> dict:
    """Where a terminal Claude runs: a herdr pane, a tmux pane, or a plain
    shell (which Steer cannot type into). Multiplexers nest and children
    inherit their variables (a tmux started inside herdr carries both), so a
    pane counts only when the process is that pane's foreground job, and the
    nearest such pane wins. Anything unconfirmed is treated as a plain shell,
    where Steer only ever signals that one process."""
    if pid in _terminal_cache:
        return _terminal_cache[pid]
    env = process_env(pid)
    candidates = []
    if env.get("HERDR_PANE_ID"):
        herdr = env.get("HERDR_BIN_PATH") if os.path.exists(env.get("HERDR_BIN_PATH") or "") else find_bin("herdr")
        if herdr:
            candidates.append({"via": "herdr", "pane": env["HERDR_PANE_ID"], "socket": env.get("HERDR_SOCKET_PATH"),
                               "bin": herdr})
    if env.get("TMUX_PANE") and env.get("TMUX"):
        tmux = find_bin("tmux")
        if tmux:
            candidates.append({"via": "tmux", "pane": env["TMUX_PANE"], "socket": env["TMUX"].split(",")[0],
                               "bin": tmux})
    term = {"via": "shell"}
    if candidates:
        pgid, tpgid, tty = process_stat(pid)
        ancestors = process_ancestors(pid)
        ranked = []
        for cand in candidates:
            shell = pane_shell_if_foreground(cand, pgid, tpgid, tty) if pgid else None
            if shell in ancestors:
                ranked.append((ancestors.index(shell), cand))
        if ranked:
            term = sorted(ranked, key=lambda r: r[0])[0][1]
    if len(_terminal_cache) > 200:
        _terminal_cache.clear()
    _terminal_cache[pid] = term
    return term


TMUX_KEYS = {"ctrl+c": "C-c", "escape": "Escape", "enter": "Enter"}
SHELLS = ("fish", "zsh", "bash", "sh", "dash", "ksh", "tcsh", "nu", "xonsh", "elvish")


def _term_env(term):
    env = dict(os.environ)
    if term.get("via") == "herdr" and term.get("socket"):
        env["HERDR_SOCKET_PATH"] = term["socket"]
    return env


async def term_command(term, args, timeout=15):
    proc = await asyncio.create_subprocess_exec(*args, env=_term_env(term), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, ""
    return proc.returncode, out.decode("utf-8", "replace")


async def terminal_keys(term, *keys):
    if term.get("via") == "herdr":
        code, _ = await term_command(term, [term["bin"], "pane", "send-keys", term["pane"]] + list(keys))
    elif term.get("via") == "tmux":
        code, _ = await term_command(term, [term["bin"], "-S", term["socket"], "send-keys", "-t", term["pane"]]
                                     + [TMUX_KEYS.get(k, k) for k in keys])
    else:
        return False
    return code == 0


async def terminal_run(term, command):
    """Types a command line into the pane's shell and presses Enter."""
    if term.get("via") == "herdr":
        code, _ = await term_command(term, [term["bin"], "pane", "run", term["pane"], command])
    elif term.get("via") == "tmux":
        code, _ = await term_command(term, [term["bin"], "-S", term["socket"], "send-keys", "-t", term["pane"],
                                            "-l", command])
        if code == 0:
            code, _ = await term_command(term, [term["bin"], "-S", term["socket"], "send-keys", "-t", term["pane"],
                                                "Enter"])
    else:
        return False
    return code == 0


async def terminal_at_shell(term) -> bool:
    """True when the pane still exists and its shell is in the foreground."""
    if term.get("via") == "herdr":
        code, out = await term_command(term, [term["bin"], "pane", "process-info", "--pane", term["pane"]])
        try:
            info = json.loads(out)["result"]["process_info"]
        except (ValueError, KeyError, TypeError):
            return False
        return code == 0 and info.get("foreground_process_group_id") == info.get("shell_pid")
    if term.get("via") == "tmux":
        code, out = await term_command(term, [term["bin"], "-S", term["socket"], "display-message", "-p", "-t",
                                              term["pane"], "#{pane_current_command}"])
        return code == 0 and os.path.basename(out.strip()).lstrip("-") in SHELLS
    return False


RESUME_FLAGS = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--verbose",
                "--chrome", "--no-chrome", "--ide", "--strict-mcp-config"}
RESUME_VALUE_FLAGS = {"--model", "--permission-mode", "--effort", "--add-dir", "--settings", "--agent",
                      "--mcp-config", "--plugin-dir", "--fallback-model"}


def resume_command(argv, session_id) -> str:
    """`claude <the terminal's own flags> --resume <id>`: keeps flags such as
    --dangerously-skip-permissions or --model, drops prompts and other
    resume/continue arguments."""
    out = ["claude"]
    i = 1
    while i < len(argv):
        arg = argv[i]
        name = arg.split("=", 1)[0]
        if name in RESUME_FLAGS:
            out.append(arg)
        elif name in RESUME_VALUE_FLAGS:
            if "=" in arg:
                out.append(arg)
            elif i + 1 < len(argv):
                out += [arg, argv[i + 1]]
                i += 1
        i += 1
    out += ["--resume", session_id]
    return " ".join(shlex.quote(a) for a in out)


SKILL_MENTION = re.compile(r"(?<![\w$])\$([A-Za-z0-9][\w:.-]*)")


def claude_command_list(info):
    """Claude's `initialize` commands: skills and custom commands first, then
    the built-in ones."""
    out = []
    for c in (info or {}).get("commands") or []:
        if not c.get("name"):
            continue
        out.append({"name": c["name"], "description": head(c.get("description") or "", 160),
                    "argumentHint": c.get("argumentHint") or "",
                    "kind": "builtin" if c.get("builtin") else "skill"})
    out.sort(key=lambda c: c["kind"] == "builtin")
    return out


class OpError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class Session:
    agent = "?"

    def __init__(self, hub, meta: dict):
        self.hub = hub
        self.meta = meta
        self.items = []
        self.index = {}
        self.pending = {}   # rid -> {"view": ..., "raw": ...}
        self.state = "idle"
        self.running_since = None
        self.updated = meta.get("updatedAt") or now_ms()
        self.loaded = False
        self.text_counts = {}
        self.activity = None
        self.last_text = meta.get("lastText") or ""
        self.error = None
        self.unsent = []
        self.suggestion = None  # the agent's guess at the next user message
        self.phone_turn = False  # the running turn was started from Steer: tell the phone how it ends

    @property
    def sid(self) -> str:
        return self.meta["sid"]

    @property
    def cwd(self) -> str:
        return self.meta.get("cwd") or HOME

    def save(self):
        if not self.meta.get("persist"):
            return
        os.makedirs(META_DIR, exist_ok=True)
        data = dict(self.meta)
        data["updatedAt"] = self.updated
        data["lastText"] = self.last_text
        tmp = os.path.join(META_DIR, self.sid + ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(META_DIR, self.sid + ".json"))

    def needs(self):
        for p in self.pending.values():
            return p["view"]["kind"]
        return None

    def summary(self) -> dict:
        state = self.state
        if self.pending:
            state = "waiting"
        return {
            "sid": self.sid,
            "agent": self.agent,
            "cwd": self.cwd,
            "title": self.meta.get("title") or "",
            "state": state,
            "needs": self.needs(),
            "lastText": self.last_text,
            "activity": self.activity,
            "updatedAt": self.updated,
            "runningSince": self.running_since,
            "model": self.meta.get("model"),
            "mode": self.meta.get("mode"),
            "source": self.meta.get("source", "steer"),
            "live": self.is_live(),
            "error": self.error,
            "suggestion": self.suggestion if state == "idle" else None,
        }

    def is_live(self) -> bool:
        return False

    def holds_helper(self) -> bool:
        """Work that would be lost if the helper restarted now."""
        return bool(self.pending) or (self.is_live() and self.state == "running")

    # timeline ---------------------------------------------------------
    def get(self, item_id):
        pos = self.index.get(item_id)
        return self.items[pos] if pos is not None else None

    def put(self, item: dict, emit=True):
        item_id = item["id"]
        if item.get("ts") is None:
            item["ts"] = now_ms()
        if item_id in self.index:
            current = self.items[self.index[item_id]]
            current.update(item)
            item = current
        else:
            self.index[item_id] = len(self.items)
            self.items.append(item)
        if emit:
            self.emit_item(item)
        return item

    def emit_item(self, item):
        if item.get("kind") == "assistant" and (item.get("text") or "").strip():
            self.last_text = one_line(item.get("text"), 160)
        if item.get("kind") == "tool":
            self.activity = {"category": item.get("category"), "target": one_line(item.get("target") or "", 80)} \
                if item.get("status") == "running" else None
        self.hub.to_watchers(self.sid, {"ev": "item", "sid": self.sid, "item": item})
        self.touch()

    def append_text(self, item_id, delta):
        item = self.get(item_id)
        if item is None:
            return
        item["text"] = (item.get("text") or "") + delta
        self.hub.to_watchers(self.sid, {"ev": "delta", "sid": self.sid, "itemId": item_id, "text": delta})
        self.last_text = one_line(item["text"][-200:], 160)
        self.touch(quiet=True)

    def notice(self, code, text=""):
        self.put({"id": new_id("n"), "kind": "notice", "code": code, "text": text})

    def add_request(self, rid, view, raw):
        view["rid"] = rid
        view.setdefault("ts", now_ms())
        self.pending[rid] = {"view": view, "raw": raw}
        self.hub.to_watchers(self.sid, {"ev": "request", "sid": self.sid, "request": view})
        self.touch()
        if self.phone_turn:
            kind = view.get("kind")
            if kind == "question":
                first = (view.get("questions") or [{}])[0]
                body = "等你回答：" + one_line(first.get("question") or "", 120)
            elif kind == "plan":
                body = "等你批准计划"
            else:
                body = "等你批准：" + one_line(view.get("target") or view.get("detail") or view.get("tool") or "", 120)
            self.hub.notify(self.push_title(), body, self.sid)

    def push_title(self):
        project = os.path.basename(self.cwd.rstrip("/")) or self.cwd
        title = self.meta.get("title") or ""
        return one_line("%s · %s" % (project, title) if title else project, 80)

    def turn_finished(self, status, detail=""):
        """Tell the phone how a turn it started ended."""
        if not self.phone_turn:
            return
        self.phone_turn = False
        if status == "interrupted":
            return
        if status == "error":
            body = "出错了：" + one_line(detail or self.error or "", 140)
        else:
            body = "做完了" + ("：" + one_line(self.last_text, 140) if self.last_text else "")
        self.hub.notify(self.push_title(), body, self.sid)

    def drop_request(self, rid):
        if self.pending.pop(rid, None) is not None:
            self.hub.to_watchers(self.sid, {"ev": "resolved", "sid": self.sid, "rid": rid})
            self.touch()

    def clear_requests(self):
        for rid in list(self.pending):
            self.drop_request(rid)

    def set_state(self, state, error=None):
        if state == "running" and self.state != "running":
            self.running_since = now_ms()
            self.suggestion = None
        if state != "running":
            self.running_since = None
            self.activity = None
        self.state = state
        self.error = error
        self.touch(urgent=True)

    def touch(self, quiet=False, urgent=False):
        self.updated = now_ms()
        self.hub.session_changed(self, quiet=quiet, urgent=urgent)

    def snapshot(self):
        items = self.items[-SNAPSHOT_ITEMS:]
        return {
            "session": self.summary(),
            "items": items,
            "earlier": max(0, len(self.items) - len(items)),
            "requests": [p["view"] for p in self.pending.values()],
        }

    async def load(self):
        self.loaded = True

    # actions (overridden) ---------------------------------------------
    async def send(self, text, images):
        raise OpError("unsupported")

    async def interrupt(self):
        raise OpError("unsupported")

    async def respond(self, rid, decision, message, answers):
        raise OpError("unsupported")

    async def set_mode(self, mode):
        self.meta["mode"] = mode
        self.save()
        self.touch()

    async def set_model(self, model):
        self.meta["model"] = model
        self.save()
        self.touch()

    async def shutdown(self):
        pass

    def adopt(self):
        """A desktop session the user acts on becomes a remembered Steer session."""
        if not self.meta.get("persist"):
            self.meta["persist"] = True
            self.save()


class ClaudeSession(Session):
    agent = "claude"

    def __init__(self, hub, meta):
        super().__init__(hub, meta)
        self.proc = None
        self.ctl_waiters = {}
        self.stderr_tail = []
        self.interrupted = False
        self.turn_started = None
        self.idle_since = time.time()
        self.cur_msg = None
        self.stream_items = {}
        self.queued = []
        self.reader_task = None
        self.closing = False        # stdin closed; the process is on its way out
        # Following the session while a terminal has it open.
        self.transcript = None
        self.transcript_path = None
        self.tail_offset = 0
        self.terminal = None        # registry entry while a terminal has it open
        self.takeover_task = None
        self.takeover_queue = []    # (item, content) waiting for the terminal to let go
        self.handback = None        # {"term", "argv"}: reopen it there once the phone is done
        self.handback_task = None

    @property
    def native_id(self):
        return self.meta.get("nativeId") or self.sid

    def is_live(self):
        return self.proc is not None and self.proc.returncode is None and not self.closing

    def holds_helper(self) -> bool:
        return super().holds_helper() or bool(self.queued or self.takeover_task or self.handback)

    def foreign_terminal(self, entry=None):
        """The registry entry of a terminal holding this session, if it is not ours."""
        entry = entry if entry is not None else claude_open_sessions().get(self.native_id)
        if entry and not (self.proc is not None and self.proc.pid == entry["pid"]):
            return entry
        return None

    def summary(self) -> dict:
        d = super().summary()
        entry = self.foreign_terminal()
        d["openInTerminal"] = bool(entry)
        if entry:
            d["terminal"] = {"via": terminal_of(entry["pid"]).get("via"), "status": entry.get("status") or ""}
            if not self.is_live() and entry.get("status") not in TERMINAL_IDLE and d["state"] == "idle":
                d["state"] = "running"
        if self.handback:
            d["handback"] = self.handback["term"].get("via")
        return d

    async def load(self):
        if self.loaded:
            return
        self.loaded = True
        path = claude_transcript_path(self.native_id, self.cwd)
        if not path:
            return
        loop = asyncio.get_event_loop()
        transcript, offset = await loop.run_in_executor(None, self._read_transcript, path)
        self.transcript_path = path
        self.tail_offset = offset
        live_items = self.items
        self.items, self.index = [], {}
        for item in transcript.items:
            self.put(item, emit=False)
        for item in live_items:
            if item["id"] not in self.index:
                self.put(item, emit=False)
        if transcript.title and not self.meta.get("titleLocked"):
            self.meta["title"] = transcript.title
        transcript.listener = self.from_transcript
        self.transcript = transcript
        for item in reversed(self.items):
            if item.get("kind") == "assistant" and (item.get("text") or "").strip():
                self.last_text = one_line(item["text"], 160)
                break

    def _read_transcript(self, path):
        transcript = ClaudeTranscript(self.cwd)
        with open(path, "rb") as f:
            data = f.read()
        cut = data.rfind(b"\n") + 1  # a half-written last line is read on the next pass
        for m in iter_json_lines(data[:cut]):
            transcript.feed(m)
        return transcript, cut

    # following a terminal -----------------------------------------------
    def from_transcript(self, item):
        self.put(item)

    def tail_transcript(self):
        """Feeds lines the terminal appended since the last look."""
        if self.transcript is None or not self.transcript_path:
            return
        try:
            size = os.path.getsize(self.transcript_path)
        except OSError:
            return
        if size < self.tail_offset:
            self.tail_offset = size
        if size == self.tail_offset:
            return
        with open(self.transcript_path, "rb") as f:
            f.seek(self.tail_offset)
            data = f.read(size - self.tail_offset)
        cut = data.rfind(b"\n") + 1
        if not cut:
            return
        self.tail_offset += cut
        for m in iter_json_lines(data[:cut]):
            try:
                self.transcript.feed(m)
            except Exception as e:
                log("transcript line skipped", self.sid, repr(e))

    def skip_transcript(self):
        """What our own process wrote is already on screen from its stream."""
        if self.transcript_path:
            try:
                self.tail_offset = os.path.getsize(self.transcript_path)
            except OSError:
                pass

    def follow_terminal(self, entry):
        entry = self.foreign_terminal(entry) if entry else None
        prev = self.terminal
        self.terminal = entry
        if (entry or prev) and self.loaded:
            self.tail_transcript()
        if (prev or {}).get("status") == (entry or {}).get("status") and bool(prev) == bool(entry):
            return
        if not self.is_live() and not self.takeover_task:
            busy = bool(entry) and entry.get("status") not in TERMINAL_IDLE
            want = "running" if busy else "idle"
            if self.state != want:
                self.set_state(want)
                return
        self.touch(urgent=True)

    async def take_over(self):
        """A terminal has this session open: wait until it is idle, then close
        it there (Ctrl+C twice in a herdr or tmux pane, SIGTERM in a plain
        shell) so only one process writes the conversation."""
        entry = self.foreign_terminal(claude_open_sessions(max_age=0).get(self.native_id))
        deadline = time.time() + TAKEOVER_WAIT
        while entry and entry.get("status") not in TERMINAL_IDLE:
            if time.time() > deadline:
                raise OpError("terminal_busy", "电脑终端里的 Claude 一直没空下来，没能接管")
            await asyncio.sleep(1)
            entry = self.foreign_terminal(claude_open_sessions(max_age=0).get(self.native_id))
        if not entry:
            return
        pid = entry["pid"]
        term = terminal_of(pid)
        argv = process_argv(pid)
        log("claude takeover", self.sid, term.get("via"), pid)
        if term["via"] in ("herdr", "tmux"):
            # The first Ctrl+C clears any half-typed input; the next one exits.
            for _ in range(3):
                await terminal_keys(term, "ctrl+c")
                if await wait_exit(pid, 1.5):
                    break
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if not await wait_exit(pid, 6):
            raise OpError("terminal_busy", "电脑终端里的 Claude 没有退出")
        if term["via"] in ("herdr", "tmux"):
            self.handback = {"term": term, "argv": argv}
        self.terminal = None
        _open_cache[0] = 0
        self.tail_transcript()
        self.touch(urgent=True)

    def schedule_handback(self):
        if not self.handback:
            return
        if self.handback_task:
            self.handback_task.cancel()
        self.handback_task = asyncio.ensure_future(self._handback_later())

    async def _handback_later(self):
        try:
            await asyncio.sleep(HANDBACK_DELAY)
        except asyncio.CancelledError:
            return
        self.handback_task = None
        handback = self.handback
        if not handback or self.state != "idle" or self.pending or self.queued or self.takeover_task:
            return
        term = handback["term"]
        if not await terminal_at_shell(term):
            log("claude handback skipped: the pane is gone or busy", self.sid)
            self.handback = None
            self.touch(urgent=True)
            return
        await self.shutdown()
        ok = await terminal_run(term, resume_command(handback["argv"], self.native_id))
        log("claude handback", self.sid, term.get("via"), "ok" if ok else "failed")
        self.handback = None
        _open_cache[0] = 0
        self.touch(urgent=True)

    async def ensure_process(self):
        if self.is_live():
            return
        old = self.proc
        if old is not None and old.returncode is None:
            # Being reaped: let it finish before starting the next one.
            try:
                await asyncio.wait_for(old.wait(), 10)
            except asyncio.TimeoutError:
                old.terminate()
                await old.wait()
            if self.reader_task:
                try:
                    await asyncio.wait_for(asyncio.shield(self.reader_task), 5)
                except Exception:
                    pass
        claude = find_bin("claude")
        if not claude:
            raise OpError("agent_missing", "claude not found on PATH")
        args = [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--permission-prompt-tool", "stdio",
                "--allow-dangerously-skip-permissions"]
        if self.meta.get("started"):
            args += ["--resume", self.native_id]
        else:
            args += ["--session-id", self.native_id]
        if self.meta.get("model"):
            args += ["--model", self.meta["model"]]
        if self.meta.get("mode"):
            args += ["--permission-mode", self.meta["mode"]]
        init = {"subtype": "initialize"}
        if await self.hub.claude_supports("--prompt-suggestions"):
            args += ["--prompt-suggestions", "true"]
            init["promptSuggestions"] = True
        if not os.path.isdir(self.cwd):
            raise OpError("cwd_missing", "这台电脑上没有这个文件夹：" + self.cwd)
        log("claude start", self.sid, " ".join(args[1:]))
        self.stderr_tail = []
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
        proc = self.proc
        self.reader_task = asyncio.ensure_future(self._read_stdout(proc))
        asyncio.ensure_future(self._read_stderr(proc))
        try:
            info = await self.control(init, timeout=90)
        except OpError:
            raise OpError("agent_failed", "\n".join(self.stderr_tail[-5:]) or "Claude did not start")
        if isinstance(info, dict):
            self.hub.remember_claude_info(info, self.cwd)
            mode = info.get("current_permission_mode")
            if mode and not self.meta.get("mode"):
                self.meta["mode"] = mode
        self.touch()

    async def _read_stderr(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail = (self.stderr_tail + [text])[-30:]

    async def _read_stdout(self, proc):
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                try:
                    self.on_message(m)
                except Exception as e:  # never let one odd message kill the session
                    log("claude message error", self.sid, repr(e))
        finally:
            await proc.wait()
            self.on_exit(proc)

    def on_exit(self, proc):
        if proc is not self.proc:
            return
        code = proc.returncode
        log("claude exit", self.sid, code)
        self.closing = False
        self.skip_transcript()
        for fut in self.ctl_waiters.values():
            if not fut.done():
                fut.set_exception(OpError("agent_exited"))
        self.ctl_waiters = {}
        self.clear_requests()
        was_running = self.state == "running"
        self.proc = None
        for item in self.items:
            if item.get("kind") == "tool" and item.get("status") == "running":
                item["status"] = "error"
                self.emit_item(item)
        if code not in (0, None) or was_running:
            detail = "\n".join(self.stderr_tail[-6:])
            self.notice("agent_exited", detail)
            self.set_state("idle", error=one_line(detail, 200) if detail else None)
            self.turn_finished("error", detail or "Claude 退出了")
        else:
            self.set_state("idle")

    async def write(self, obj):
        if not self.is_live():
            raise OpError("agent_not_running")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        except (ConnectionError, BrokenPipeError, OSError):
            self.closing = True
            raise OpError("agent_not_running", "Claude 进程刚好退出了")

    async def control(self, request, timeout=30):
        rid = "steer-" + uuid.uuid4().hex[:10]
        fut = asyncio.get_event_loop().create_future()
        self.ctl_waiters[rid] = fut
        await self.write({"type": "control_request", "request_id": rid, "request": request})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise OpError("timeout", "control request timed out")
        finally:
            self.ctl_waiters.pop(rid, None)

    # stream ----------------------------------------------------------
    def on_message(self, m):
        t = m.get("type")
        if t == "control_response":
            resp = m.get("response") or {}
            fut = self.ctl_waiters.get(resp.get("request_id"))
            if fut and not fut.done():
                if resp.get("subtype") == "error":
                    fut.set_exception(OpError("agent_error", resp.get("error") or "error"))
                else:
                    fut.set_result(resp.get("response"))
            return
        if t == "control_request":
            self.on_control_request(m)
            return
        if t == "control_cancel_request":
            self.drop_request(m.get("request_id"))
            return
        if m.get("parent_tool_use_id"):
            return  # sub-agent chatter stays inside its Task row
        if t == "system":
            sub = m.get("subtype")
            if sub == "init":
                changed = False
                if m.get("model") and m.get("model") != self.meta.get("reportedModel"):
                    self.meta["reportedModel"] = m.get("model")
                    changed = True
                if m.get("slash_commands"):
                    self.hub.remember_claude_commands(m.get("slash_commands"))
                if changed:
                    self.save()
            elif sub == "status":
                if m.get("permissionMode"):
                    self.meta["mode"] = m.get("permissionMode")
                    self.save()
                    self.touch()
                if m.get("status") == "requesting" and self.state != "running":
                    self.set_state("running")
            elif sub == "compact_boundary":
                self.notice("compacted")
            return
        if t == "stream_event":
            self.on_stream_event(m.get("event") or {})
            return
        if t == "assistant":
            msg = m.get("message") or {}
            msg_id = msg.get("id") or new_id("m")
            for block in msg.get("content") or []:
                item_id = add_assistant_block(self, msg_id, block, self.cwd, None, final=True)
            return
        if t == "user":
            msg = m.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                result = m.get("tool_use_result", m.get("toolUseResult"))
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        apply_tool_result(self, b, result, self.cwd, None)
            return
        if t == "result":
            self.on_result(m)
            return
        if t == "prompt_suggestion":
            text = (m.get("suggestion") or "").strip()
            if text and self.state == "idle" and not self.pending and not self.queued:
                self.suggestion = head(text, 300)
                self.hub.session_changed(self, urgent=True)
            return

    def on_stream_event(self, ev):
        et = ev.get("type")
        if et == "message_start":
            self.cur_msg = (ev.get("message") or {}).get("id")
            self.stream_items = {}
            if self.state != "running":
                self.set_state("running")
        elif et == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "text" and self.cur_msg:
                n = self.text_counts.get(self.cur_msg, 0)
                item_id = "%s:t%d" % (self.cur_msg, n)
                self.stream_items[ev.get("index")] = item_id
                if self.get(item_id) is None:
                    self.put({"id": item_id, "kind": "assistant", "text": "", "streaming": True})
            elif block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                view = claude_tool_view(name, {}, self.cwd)
                if view["category"] not in ("meta", "todo") and name != "TodoWrite":
                    item = {"id": block.get("id"), "kind": "tool", "status": "running", "pending": True}
                    item.update(view)
                    if self.get(block.get("id")) is None:
                        self.put(item)
        elif et == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                item_id = self.stream_items.get(ev.get("index"))
                if item_id:
                    self.append_text(item_id, delta.get("text") or "")

    def on_control_request(self, m):
        req = m.get("request") or {}
        rid = m.get("request_id")
        if req.get("subtype") != "can_use_tool":
            # Hooks or MCP callbacks we do not host: answer so the agent never hangs.
            asyncio.ensure_future(self.write({"type": "control_response", "response": {
                "subtype": "error", "request_id": rid, "error": "not supported by Steer"}}))
            return
        name = req.get("tool_name") or ""
        inp = req.get("input") or {}
        if name == "AskUserQuestion":
            questions = []
            for i, q in enumerate(inp.get("questions") or []):
                questions.append({
                    "id": q.get("question") or str(i),
                    "header": q.get("header") or "",
                    "question": q.get("question") or "",
                    "options": [{"label": o.get("label", ""), "description": o.get("description", "")} for o in q.get("options") or []],
                    "multiSelect": bool(q.get("multiSelect")),
                    "allowOther": True,
                })
            view = {"kind": "question", "questions": questions}
        elif name == "ExitPlanMode":
            view = {"kind": "plan", "plan": inp.get("plan", "")}
        else:
            view = {"kind": "permission"}
            view.update(claude_tool_view(name, inp, self.cwd))
            view["reason"] = req.get("decision_reason") or req.get("description") or ""
            view["canAllowSession"] = True
        self.add_request(rid, view, req)

    def on_result(self, m):
        status = "completed"
        if self.interrupted:
            status = "interrupted"
        elif m.get("is_error") or (m.get("subtype") or "").startswith("error"):
            status = "error"
        detail = ""
        if status == "error":
            detail = m.get("result") or ", ".join(m.get("errors") or []) or m.get("subtype") or ""
        self.put({"id": new_id("turn"), "kind": "turn", "status": status, "durationMs": m.get("duration_ms"),
                  "costUsd": m.get("total_cost_usd"), "text": one_line(detail, 300)})
        self.interrupted = False
        for item in self.items:
            if item.get("kind") == "tool" and item.get("status") == "running":
                item["status"] = "error" if status == "error" else "ok"
                self.emit_item(item)
            if item.get("kind") == "assistant" and item.get("streaming"):
                item["streaming"] = False
        self.clear_requests()
        self.idle_since = time.time()
        if self.queued:
            # Claude takes the next queued message as its own turn right away.
            item = self.get(self.queued.pop(0))
            if item:
                item["queued"] = False
                self.emit_item(item)
            self.set_state("running")
            return
        self.skip_transcript()
        self.set_state("idle")
        self.turn_finished(status, detail)
        self.schedule_handback()

    # actions ---------------------------------------------------------
    async def send(self, text, images):
        await self.load()
        self.suggestion = None
        self.phone_turn = True
        if self.handback_task:
            self.handback_task.cancel()
            self.handback_task = None
        if images:
            content = [{"type": "image", "source": {"type": "base64", "media_type": img.get("mime", "image/png"),
                                                    "data": img.get("data", "")}} for img in images]
            if text:
                content.append({"type": "text", "text": text})
        else:
            content = text
        if self.takeover_task or self.foreign_terminal(claude_open_sessions(max_age=0).get(self.native_id)):
            item = {"id": new_id("u"), "kind": "user", "text": text, "images": len(images or []), "queued": True}
            self.put(item)
            self.takeover_queue.append((item, content))
            if not self.takeover_task:
                self.takeover_task = asyncio.ensure_future(self._take_over_and_send())
            self.mark_started(text)
            return {"id": item["id"]}
        busy = self.state == "running" or bool(self.pending)
        item = {"id": new_id("u"), "kind": "user", "text": text, "images": len(images or []), "queued": busy}
        self.put(item)
        if busy:
            self.queued.append(item["id"])
        try:
            for attempt in (1, 2):
                try:
                    await self.ensure_process()
                    await self.write({"type": "user", "message": {"role": "user", "content": content}})
                    break
                except OpError as e:
                    if e.code != "agent_not_running" or attempt == 2:
                        raise
                    log("claude went away mid-send; starting it again", self.sid)
        except OpError as e:
            item["failed"] = True
            self.emit_item(item)
            self.notice("send_failed", e.message)
            raise
        self.mark_started(text)
        if not busy:
            self.set_state("running")
        return {"id": item["id"]}

    def mark_started(self, text):
        if not self.meta.get("started"):
            self.meta["started"] = True
        if not self.meta.get("title"):
            self.meta["title"] = one_line(text, 60)
        self.adopt()
        self.save()

    async def _take_over_and_send(self):
        try:
            await self.take_over()
            if self.is_live():
                # Our own process missed what the terminal added; start it afresh.
                await self.shutdown()
            await self.ensure_process()
            first = True
            while self.takeover_queue:
                item, content = self.takeover_queue[0]
                await self.write({"type": "user", "message": {"role": "user", "content": content}})
                self.takeover_queue.pop(0)
                if first:
                    item["queued"] = False
                    self.emit_item(item)
                    self.set_state("running")
                    first = False
                else:
                    self.queued.append(item["id"])
        except OpError as e:
            for item, _ in self.takeover_queue:
                item["failed"] = True
                item["queued"] = False
                self.emit_item(item)
            self.takeover_queue = []
            self.notice("send_failed", e.message)
        finally:
            self.takeover_task = None
            self.touch(urgent=True)

    async def interrupt(self):
        entry = self.foreign_terminal(claude_open_sessions(max_age=0).get(self.native_id))
        if entry and not self.is_live():
            term = terminal_of(entry["pid"])
            if term["via"] in ("herdr", "tmux") and await terminal_keys(term, "escape"):
                return {}
            raise OpError("in_terminal", "这个会话正在电脑终端里跑，手机上停不了")
        if not self.is_live():
            return {}
        self.interrupted = True
        await self.control({"subtype": "interrupt"}, timeout=15)
        return {}

    async def respond(self, rid, decision, message, answers):
        entry = self.pending.get(rid)
        if entry is None:
            raise OpError("unknown_request")
        view, raw = entry["view"], entry["raw"]
        inp = raw.get("input") or {}
        if view["kind"] == "question":
            if decision == "deny":
                resp = {"behavior": "deny", "message": message or "The user dismissed the question."}
            else:
                mapped = {}
                for q in view.get("questions") or []:
                    ans = (answers or {}).get(q["id"])
                    if isinstance(ans, list):
                        ans = ", ".join(ans)
                    if ans:
                        mapped[q["question"]] = ans
                updated = dict(inp)
                updated["answers"] = mapped
                resp = {"behavior": "allow", "updatedInput": updated}
        elif view["kind"] == "plan":
            if decision == "deny":
                resp = {"behavior": "deny", "message": message or "The user wants to keep planning."}
                if not message:
                    resp["interrupt"] = True
            else:
                resp = {"behavior": "allow", "updatedInput": inp}
                if decision == "allowSession":
                    resp["updatedPermissions"] = [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
        else:
            if decision == "deny":
                resp = {"behavior": "deny", "message": message or "The user declined this action."}
                if not message:
                    resp["interrupt"] = True
            else:
                resp = {"behavior": "allow", "updatedInput": inp}
                if decision == "allowSession":
                    suggestions = []
                    for s in raw.get("permission_suggestions") or []:
                        s = dict(s)
                        s["destination"] = "session"
                        suggestions.append(s)
                    if not suggestions:
                        suggestions = [{"type": "addRules", "rules": [{"toolName": raw.get("tool_name")}],
                                        "behavior": "allow", "destination": "session"}]
                    resp["updatedPermissions"] = suggestions
        await self.write({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": resp}})
        if decision == "deny" and not message:
            self.interrupted = True
        self.drop_request(rid)
        return {}

    async def set_mode(self, mode):
        self.meta["mode"] = mode
        if self.is_live():
            await self.control({"subtype": "set_permission_mode", "mode": mode})
        self.save()
        self.touch()

    async def set_model(self, model):
        self.meta["model"] = model
        if self.is_live():
            await self.control({"subtype": "set_model", "model": model})
        self.save()
        self.touch()

    async def shutdown(self):
        proc = self.proc
        if proc and proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.terminate()

    def reap_if_idle(self):
        if self.is_live() and self.state == "idle" and not self.pending and time.time() - self.idle_since > CLAUDE_IDLE_EXIT:
            log("claude idle exit", self.sid)
            self.closing = True
            try:
                self.proc.stdin.close()
            except Exception:
                pass


CODEX_MODES = {
    # mode -> (approvalPolicy, sandboxPolicy)
    "untrusted": ("untrusted", {"type": "workspaceWrite"}),
    "auto": ("on-request", {"type": "workspaceWrite"}),
    "readOnly": ("on-request", {"type": "readOnly"}),
    "full": ("never", {"type": "dangerFullAccess"}),
}
CODEX_SANDBOX_MODE = {"untrusted": "workspace-write", "auto": "workspace-write", "readOnly": "read-only",
                      "full": "danger-full-access"}


def ws_mask(key: bytes, data: bytes) -> bytes:
    if not data:
        return data
    pad = (key * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(len(data), "big")


def ws_frame(opcode: int, payload: bytes, mask=True) -> bytes:
    """One final WebSocket frame. Clients must mask what they send."""
    n = len(payload)
    bit = 0x80 if mask else 0
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([bit | n])
    elif n < 65536:
        head += bytes([bit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([bit | 127]) + struct.pack(">Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + ws_mask(key, payload)


class WSConn:
    """A small WebSocket client over a Unix socket: enough for Codex's
    app-server control socket, which speaks JSON-RPC in text messages."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.closed = False

    @classmethod
    async def open_unix(cls, path, timeout=10):
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise OSError("websocket upgrade refused: " + head.split(b"\r\n", 1)[0].decode("latin-1"))
        return cls(reader, writer)

    async def send(self, text: str):
        if self.closed:
            raise OpError("agent_not_running")
        self.writer.write(ws_frame(1, text.encode("utf-8")))
        await self.writer.drain()

    async def recv(self):
        """The next text message, or None once the connection is gone."""
        parts = []
        while True:
            try:
                b0, b1 = await self.reader.readexactly(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                key = await self.reader.readexactly(4) if b1 & 0x80 else None
                payload = await self.reader.readexactly(n) if n else b""
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                return None
            if key:
                payload = ws_mask(key, payload)
            op = b0 & 0x0F
            if op == 8:
                self.closed = True
                return None
            if op == 9:
                self.writer.write(ws_frame(10, payload))
                continue
            if op == 10:
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass


async def run_capture(args, timeout=30):
    """Runs a short command; returns (exit code, stdout text)."""
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, ""
    return proc.returncode, out.decode("utf-8", "replace")


class CodexBackend:
    """One JSON-RPC connection to Codex's app-server. Prefers the shared
    app-server daemon (the one `codex` in a terminal also joins), so a thread
    open in a terminal and in Steer is one live thread; falls back to a private
    `codex app-server` child when the daemon is unavailable."""

    def __init__(self, hub):
        self.hub = hub
        self.proc = None
        self.ws = None
        self.via = None
        self.next_id = 0
        self.waiters = {}
        self.generation = 0
        self.lock = asyncio.Lock()
        self.models = None

    def alive(self):
        if self.ws is not None:
            return not self.ws.closed
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self):
        async with self.lock:
            if self.alive():
                return
            codex = find_bin("codex")
            if not codex:
                raise OpError("agent_missing", "codex not found on PATH")
            ws = await self._open_daemon(codex) if os.environ.get("STEER_CODEX_DAEMON", "1") != "0" else None
            self.generation += 1
            if ws is not None:
                log("codex: using the shared app-server daemon")
                self.ws, self.proc, self.via = ws, None, "daemon"
                asyncio.ensure_future(self._read_ws(ws))
            else:
                log("codex app-server start")
                self.ws, self.via = None, "stdio"
                self.proc = await asyncio.create_subprocess_exec(
                    codex, "app-server", cwd=HOME, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
                proc = self.proc
                asyncio.ensure_future(self._read(proc))
                asyncio.ensure_future(self._drain_stderr(proc))
            await self._request("initialize", {"clientInfo": {"name": "steer", "title": "Steer", "version": VERSION}})
            await self._send({"method": "initialized"})

    async def _daemon_info(self, codex):
        code, out = await run_capture([codex, "app-server", "daemon", "version"], 20)
        try:
            return json.loads(out) if code == 0 else {}
        except ValueError:
            return {}

    async def _open_daemon(self, codex):
        try:
            info = await self._daemon_info(codex)
            if info.get("status") != "running":
                await run_capture([codex, "app-server", "daemon", "start"], 60)
                info = await self._daemon_info(codex)
            path = info.get("socketPath")
            if info.get("status") != "running" or not path:
                return None
            return await WSConn.open_unix(path)
        except Exception as e:
            log("codex daemon unavailable:", repr(e))
            return None

    async def _drain_stderr(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break

    async def _send(self, obj):
        if not self.alive():
            raise OpError("agent_not_running")
        data = json.dumps(obj, ensure_ascii=False)
        if self.ws is not None:
            await self.ws.send(data)
        else:
            self.proc.stdin.write((data + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def _request(self, method, params, timeout=90):
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_event_loop().create_future()
        self.waiters[rid] = fut
        await self._send({"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise OpError("timeout", method + " timed out")
        finally:
            self.waiters.pop(rid, None)

    async def request(self, method, params, timeout=90):
        await self.ensure()
        return await self._request(method, params, timeout)

    async def respond(self, rid, result):
        await self._send({"id": rid, "result": result})

    def _dispatch(self, m):
        try:
            self.on_message(m)
        except Exception as e:
            log("codex message error", repr(e))

    async def _read(self, proc):
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                self._dispatch(m)
        finally:
            await proc.wait()
            log("codex app-server exit", proc.returncode)
            if proc is self.proc:
                self.proc = None
                self._lost()

    async def _read_ws(self, ws):
        try:
            while True:
                text = await ws.recv()
                if text is None:
                    break
                try:
                    m = json.loads(text)
                except Exception:
                    continue
                self._dispatch(m)
        finally:
            log("codex daemon connection closed")
            if ws is self.ws:
                self.ws = None
                self._lost()

    def _lost(self):
        for fut in self.waiters.values():
            if not fut.done():
                fut.set_exception(OpError("agent_exited"))
        self.waiters = {}
        for sess in self.hub.sessions.values():
            if isinstance(sess, CodexSession):
                sess.on_backend_exit()

    async def close(self):
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        elif self.alive():
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:
                self.proc.terminate()

    def on_message(self, m):
        if "method" not in m:
            fut = self.waiters.get(m.get("id"))
            if fut and not fut.done():
                if "error" in m:
                    err = m.get("error") or {}
                    fut.set_exception(OpError("agent_error", err.get("message") or json.dumps(err)))
                else:
                    fut.set_result(m.get("result"))
            return
        params = m.get("params") or {}
        thread_id = params.get("threadId")
        session = self.hub.sessions.get(thread_id) if thread_id else None
        if "id" in m:
            if isinstance(session, CodexSession):
                session.on_server_request(m)
            else:
                asyncio.ensure_future(self._send({"id": m["id"], "error": {"code": -32601, "message": "not handled"}}))
            return
        if isinstance(session, CodexSession):
            session.on_notification(m.get("method"), params)


class CodexSession(Session):
    agent = "codex"

    def __init__(self, hub, meta):
        super().__init__(hub, meta)
        self.turn_id = None
        self.generation = 0
        self.interrupted = False
        self.output_flush = set()

    def is_live(self):
        b = self.hub.codex
        return b.alive() and self.generation == b.generation

    def holds_helper(self) -> bool:
        # On the shared daemon a running turn carries on without us.
        if self.hub.codex.via == "daemon":
            return bool(self.pending)
        return super().holds_helper()

    async def load(self):
        if self.loaded:
            return
        self.loaded = True
        if not self.meta.get("started"):
            return
        try:
            r = await self.hub.codex.request("thread/read", {"threadId": self.sid, "includeTurns": True})
        except OpError as e:
            log("codex thread/read failed", self.sid, e.message)
            return
        thread = (r or {}).get("thread") or {}
        if thread.get("name") and not self.meta.get("titleLocked"):
            self.meta["title"] = thread.get("name")
        live_items = self.items
        self.items, self.index = [], {}
        for turn in thread.get("turns") or []:
            for it in turn.get("items") or []:
                norm = codex_item(it, self.cwd, {})
                if norm:
                    norm["streaming"] = False if norm.get("kind") == "assistant" else norm.get("streaming")
                    self.put(norm, emit=False)
            if turn.get("status") in ("completed", "interrupted", "failed"):
                self.put({"id": "turn-" + turn.get("id", new_id("t")), "kind": "turn",
                          "status": {"failed": "error"}.get(turn.get("status"), turn.get("status")),
                          "durationMs": turn.get("durationMs"), "ts": (turn.get("completedAt") or 0) * 1000 or None},
                         emit=False)
            elif turn.get("status") == "inProgress":
                self.turn_id = turn.get("id")
        for item in live_items:
            if item["id"] not in self.index:
                self.put(item, emit=False)
        for item in reversed(self.items):
            if item.get("kind") == "assistant" and (item.get("text") or "").strip():
                self.last_text = one_line(item["text"], 160)
                break
        if self.hub.codex.via == "daemon":
            # Follow the thread live: a terminal on the same daemon may be driving it.
            try:
                await self.ensure_thread()
            except OpError as e:
                log("codex follow failed", self.sid, e.message)
            if self.turn_id and self.state != "running":
                self.set_state("running")

    def policy(self):
        mode = self.meta.get("mode") or "auto"
        return CODEX_MODES.get(mode, CODEX_MODES["auto"])

    async def create_thread(self):
        approval, _ = self.policy()
        params = {"cwd": self.cwd, "approvalPolicy": approval,
                  "sandbox": CODEX_SANDBOX_MODE.get(self.meta.get("mode") or "auto", "workspace-write")}
        if self.meta.get("model"):
            params["model"] = self.meta["model"]
        r = await self.hub.codex.request("thread/start", params)
        thread = r["thread"]
        self.generation = self.hub.codex.generation
        return thread["id"]

    async def ensure_thread(self):
        """Loads and subscribes to the thread. On the shared daemon another
        client (a terminal) may have it open too, so this sets nothing: the
        policy, model and sandbox travel with each turn/start instead."""
        if self.is_live():
            return
        await self.hub.codex.ensure()
        await self.hub.codex.request("thread/resume", {"threadId": self.sid, "excludeTurns": True})
        self.generation = self.hub.codex.generation

    def on_backend_exit(self):
        self.generation = 0
        self.clear_requests()
        if self.state == "running":
            self.notice("agent_exited", "codex app-server stopped")
            self.set_state("idle")
        self.turn_id = None

    def on_notification(self, method, p):
        if method == "turn/started":
            self.turn_id = (p.get("turn") or {}).get("id")
            self.interrupted = False
            self.set_state("running")
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            status = {"failed": "error"}.get(turn.get("status"), turn.get("status") or "completed")
            err = turn.get("error") or {}
            self.put({"id": "turn-" + (turn.get("id") or new_id("t")), "kind": "turn", "status": status,
                      "durationMs": turn.get("durationMs"), "text": one_line(err.get("message") or "", 300)})
            self.turn_id = None
            self.clear_requests()
            for item in self.items:
                if item.get("kind") == "assistant" and item.get("streaming"):
                    item["streaming"] = False
                    self.emit_item(item)
            self.set_state("idle")
            self.turn_finished(status, err.get("message") or "")
        elif method in ("item/started", "item/completed"):
            norm = codex_item(p.get("item") or {}, self.cwd, {})
            if norm:
                if norm.get("kind") == "user":
                    existing = self.get(norm["id"])
                    if existing:
                        existing.pop("queued", None)
                if method == "item/completed" and norm.get("kind") == "assistant":
                    norm["streaming"] = False
                self.put(norm)
        elif method == "item/agentMessage/delta":
            item_id = p.get("itemId")
            if self.get(item_id) is None:
                self.put({"id": item_id, "kind": "assistant", "text": "", "streaming": True})
            self.append_text(item_id, p.get("delta") or "")
        elif method == "item/commandExecution/outputDelta":
            item = self.get(p.get("itemId"))
            if item is not None:
                item["output"] = tail((item.get("output") or "") + (p.get("delta") or ""), OUTPUT_TAIL)
                self.schedule_flush(item["id"])
        elif method == "turn/plan/updated":
            steps = [{"step": s.get("step", ""), "status": s.get("status", "pending")} for s in p.get("plan") or []]
            self.put({"id": "plan-" + (p.get("turnId") or ""), "kind": "plan", "steps": steps,
                      "text": p.get("explanation") or ""})
        elif method == "turn/diff/updated":
            self.meta["lastDiff"] = head(p.get("diff") or "", DIFF_LIMIT)
        elif method == "thread/name/updated":
            if p.get("threadName") or p.get("name"):
                self.meta["title"] = p.get("threadName") or p.get("name")
                self.save()
                self.touch()
        elif method == "serverRequest/resolved":
            self.drop_request("x%s" % p.get("requestId"))
        elif method == "error":
            err = p.get("error") or {}
            if not p.get("willRetry"):
                self.notice("agent_error", err.get("message") or "")

    def schedule_flush(self, item_id):
        if item_id in self.output_flush:
            return
        self.output_flush.add(item_id)

        def flush():
            self.output_flush.discard(item_id)
            item = self.get(item_id)
            if item is not None:
                self.emit_item(item)
        asyncio.get_event_loop().call_later(0.4, flush)

    def on_server_request(self, m):
        method = m.get("method")
        p = m.get("params") or {}
        rid = "x%s" % m.get("id")
        if method == "item/commandExecution/requestApproval":
            command = strip_shell_wrapper(p.get("command") or "")
            view = {"kind": "permission", "tool": "shell", "category": "command", "target": command,
                    "detail": rel_path(p.get("cwd") or "", self.cwd), "reason": p.get("reason") or "",
                    "canAllowSession": True}
            if p.get("networkApprovalContext"):
                view["category"] = "network"
                view["target"] = (p.get("networkApprovalContext") or {}).get("host", "")
        elif method == "item/fileChange/requestApproval":
            item = self.get(p.get("itemId")) or {}
            view = {"kind": "permission", "tool": "apply_patch", "category": "edit",
                    "target": item.get("target", ""), "diff": item.get("diff", ""), "add": item.get("add"),
                    "del": item.get("del"), "reason": p.get("reason") or "", "canAllowSession": True}
        elif method == "item/permissions/requestApproval":
            view = {"kind": "permission", "tool": "permissions", "category": "permissions",
                    "target": one_line(json.dumps(p.get("permissions") or {}, ensure_ascii=False), 200),
                    "reason": p.get("reason") or "", "canAllowSession": True}
        elif method == "item/tool/requestUserInput":
            questions = []
            for q in p.get("questions") or []:
                questions.append({"id": q.get("id"), "header": q.get("header") or "", "question": q.get("question") or "",
                                  "options": [{"label": o.get("label", ""), "description": o.get("description", "")}
                                              for o in q.get("options") or []],
                                  "multiSelect": False, "allowOther": bool(q.get("isOther")) or not q.get("options"),
                                  "secret": bool(q.get("isSecret"))})
            view = {"kind": "question", "questions": questions}
        elif method == "mcpServer/elicitation/request":
            asyncio.ensure_future(self.hub.codex.respond(m["id"], {"action": "decline"}))
            self.notice("elicitation_declined", p.get("message") or "")
            return
        else:
            asyncio.ensure_future(self.hub.codex._send({"id": m["id"], "error": {"code": -32601, "message": "not supported by Steer"}}))
            return
        self.add_request(rid, view, m)

    async def send(self, text, images):
        await self.load()
        self.phone_turn = True
        item_id = new_id("u")
        steering = self.state == "running" and self.turn_id is not None
        self.put({"id": item_id, "kind": "user", "text": text, "images": len(images or []), "queued": False})
        try:
            await self.ensure_thread()
            inputs = []
            if text:
                inputs.append({"type": "text", "text": text, "text_elements": []})
            for img in images or []:
                inputs.append({"type": "localImage", "path": save_upload(img)})
            inputs += await self.skill_inputs(text)
            if steering and self.is_live() and self.turn_id:
                await self.hub.codex.request("turn/steer", {"threadId": self.sid, "input": inputs,
                                                            "expectedTurnId": self.turn_id,
                                                            "clientUserMessageId": item_id})
            else:
                approval, sandbox = self.policy()
                params = {"threadId": self.sid, "input": inputs, "clientUserMessageId": item_id,
                          "approvalPolicy": approval, "sandboxPolicy": sandbox}
                if self.meta.get("model"):
                    params["model"] = self.meta["model"]
                if self.meta.get("effort"):
                    params["effort"] = self.meta["effort"]
                r = await self.hub.codex.request("turn/start", params)
                self.turn_id = ((r or {}).get("turn") or {}).get("id") or self.turn_id
                self.set_state("running")
        except OpError as e:
            item = self.get(item_id)
            item["failed"] = True
            self.emit_item(item)
            self.notice("send_failed", e.message)
            raise
        self.meta["started"] = True
        if not self.meta.get("title"):
            self.meta["title"] = one_line(text, 60)
        self.adopt()
        self.save()
        return {"id": item_id}

    async def skill_inputs(self, text):
        """`$name` in the text attaches that skill, as the Codex TUI does."""
        names = [n.rstrip(".:-") for n in SKILL_MENTION.findall(text or "")]
        if not names:
            return []
        try:
            skills = await self.hub.codex_skill_list(self.cwd)
        except OpError:
            return []
        paths = {s["name"]: s["path"] for s in skills if s.get("name") and s.get("path")}
        return [{"type": "skill", "name": n, "path": paths[n]} for n in dict.fromkeys(names) if n in paths]

    async def interrupt(self):
        if self.turn_id and self.is_live():
            await self.hub.codex.request("turn/interrupt", {"threadId": self.sid, "turnId": self.turn_id})
        return {}

    async def respond(self, rid, decision, message, answers):
        entry = self.pending.get(rid)
        if entry is None:
            raise OpError("unknown_request")
        view, raw = entry["view"], entry["raw"]
        method = raw.get("method")
        if method == "item/tool/requestUserInput":
            result = {"answers": {}}
            for q in view.get("questions") or []:
                ans = (answers or {}).get(q["id"])
                if ans is None:
                    continue
                result["answers"][q["id"]] = {"answers": ans if isinstance(ans, list) else [ans]}
        elif method == "item/permissions/requestApproval":
            if decision == "deny":
                result = {"permissions": {}}
            else:
                result = {"permissions": (raw.get("params") or {}).get("permissions") or {},
                          "scope": "session" if decision == "allowSession" else "turn"}
        else:
            if decision == "allow":
                result = {"decision": "accept"}
            elif decision == "allowSession":
                result = {"decision": "acceptForSession"}
            else:
                result = {"decision": "decline" if message else "cancel"}
        await self.hub.codex.respond(raw["id"], result)
        self.drop_request(rid)
        if decision == "deny" and message and self.turn_id:
            try:
                await self.hub.codex.request("turn/steer", {"threadId": self.sid, "expectedTurnId": self.turn_id,
                                                            "input": [{"type": "text", "text": message, "text_elements": []}]})
            except OpError:
                pass
        return {}

    async def rename(self, title):
        if self.meta.get("started"):
            try:
                await self.hub.codex.request("thread/name/set", {"threadId": self.sid, "name": title})
            except OpError:
                pass


def save_upload(img: dict) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/heic": ".heic", "image/gif": ".gif",
           "image/webp": ".webp"}.get(img.get("mime"), ".png")
    path = os.path.join(UPLOAD_DIR, uuid.uuid4().hex + ext)
    with open(path, "wb") as f:
        f.write(base64.b64decode(img.get("data") or ""))
    return path


def prune_uploads():
    cutoff = time.time() - 7 * 86400
    for path in glob.glob(os.path.join(UPLOAD_DIR, "*")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Files and folders
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", "node_modules", ".build", "build", "dist", "DerivedData", ".venv", "venv", "__pycache__",
             ".next", ".cache", "Pods", "target", ".idea", ".gradle"}
_file_cache = {}


def list_project_files(cwd: str):
    cached = _file_cache.get(cwd)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    files = []
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
        if out.returncode == 0:
            files = [f for f in out.stdout.decode("utf-8", "replace").splitlines() if f]
    except Exception:
        files = []
    if not files:
        for root, dirs, names in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for n in names:
                files.append(os.path.relpath(os.path.join(root, n), cwd))
                if len(files) >= 30000:
                    break
            if len(files) >= 30000:
                break
    _file_cache[cwd] = (time.time(), files)
    return files


def fuzzy_score(query: str, path: str):
    q = query.lower()
    p = path.lower()
    base = os.path.basename(p)
    if q in base:
        return 1000 - len(base) + (200 if base.startswith(q) else 0)
    if q in p:
        return 600 - len(p)
    pos = 0
    score = 0
    last = -2
    for ch in q:
        found = p.find(ch, pos)
        if found < 0:
            return None
        score += 8 if found == last + 1 else 1
        last = found
        pos = found + 1
    return score - len(p) * 0.05


def search_files(cwd: str, query: str, limit=40):
    files = list_project_files(cwd)
    if not query:
        return files[:limit]
    scored = []
    for f in files:
        s = fuzzy_score(query, f)
        if s is not None:
            scored.append((s, f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:limit]]


def list_dirs(path: str):
    path = os.path.expanduser(path or "~")
    path = os.path.abspath(path)
    entries = []
    try:
        names = sorted(os.listdir(path), key=lambda n: n.lower())
    except OSError as e:
        raise OpError("not_found", str(e))
    for n in names:
        if n.startswith("."):
            continue
        full = os.path.join(path, n)
        if os.path.isdir(full):
            entries.append({"name": n, "path": full, "git": os.path.isdir(os.path.join(full, ".git"))})
        if len(entries) >= 400:
            break
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "entries": entries,
            "git": os.path.isdir(os.path.join(path, ".git"))}


def make_dir(parent: str, name: str):
    """Creates one folder `name` inside `parent`. A folder already there is
    returned as is: the user wanted that folder, and now it exists."""
    name = (name or "").strip()
    if not name or "/" in name or "\0" in name or name in (".", ".."):
        raise OpError("bad_name", "文件夹名不能为空，也不能包含 /")
    parent = os.path.abspath(os.path.expanduser(parent or "~"))
    if not os.path.isdir(parent):
        raise OpError("not_found", "这台电脑上没有这个文件夹：" + parent)
    full = os.path.join(parent, name)
    try:
        os.mkdir(full)
    except FileExistsError:
        if not os.path.isdir(full):
            raise OpError("exists", "这里已经有一个同名文件：" + name)
    except OSError as e:
        raise OpError("mkdir_failed", "没能新建文件夹：" + (e.strerror or str(e)))
    return {"path": full}


# ---------------------------------------------------------------------------
# Hub: clients, sessions, ops
# ---------------------------------------------------------------------------

def der_ecdsa_to_raw(der: bytes) -> bytes:
    """openssl writes ECDSA signatures as DER; a JWT wants r and s side by side."""
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    parts = []
    for _ in range(2):
        if der[i] != 2:
            raise ValueError("not an ECDSA signature")
        n = der[i + 1]
        parts.append(der[i + 2:i + 2 + n].lstrip(b"\0").rjust(32, b"\0"))
        i += 2 + n
    return parts[0] + parts[1]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Pusher:
    """Sends notifications straight to Apple's push service (APNs) with the
    team's APNs key, using the system's openssl (ES256) and curl (HTTP/2):
    no relay. The key lives in ~/.local/share/steer/apns/ (`steerd.py
    push-key`); phones register their device tokens with `push.register`."""

    def __init__(self):
        self.devices = {}
        self.jwt = (0.0, None, None)
        try:
            with open(PUSH_DEVICES) as f:
                self.devices = json.load(f)
        except Exception:
            self.devices = {}

    def config(self):
        try:
            with open(os.path.join(APNS_DIR, "apns.json")) as f:
                cfg = json.load(f)
        except Exception:
            return None
        key = os.path.join(APNS_DIR, "AuthKey_%s.p8" % cfg.get("keyId"))
        if not cfg.get("keyId") or not cfg.get("teamId") or not os.path.exists(key):
            return None
        cfg["keyPath"] = key
        return cfg

    def configured(self) -> bool:
        return self.config() is not None

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PUSH_DEVICES + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.devices, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PUSH_DEVICES)

    def register(self, token, env, host, topic):
        token = "".join(ch for ch in str(token or "") if ch in "0123456789abcdefABCDEF").lower()
        if not token:
            raise OpError("bad_token", "empty device token")
        self.devices[token] = {"env": "sandbox" if env == "sandbox" else "production", "host": host or "",
                               "topic": topic or "com.kadaliao.steer", "updatedAt": now_ms()}
        self.save()

    async def bearer(self, cfg):
        made, key_id, token = self.jwt
        if token and key_id == cfg["keyId"] and time.time() - made < 40 * 60:
            return token
        head = b64url(json.dumps({"alg": "ES256", "kid": cfg["keyId"]}).encode())
        body = b64url(json.dumps({"iss": cfg["teamId"], "iat": int(time.time())}).encode())
        proc = await asyncio.create_subprocess_exec(
            find_bin("openssl") or "openssl", "dgst", "-sha256", "-sign", cfg["keyPath"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        der, _ = await asyncio.wait_for(proc.communicate(("%s.%s" % (head, body)).encode()), 15)
        token = "%s.%s.%s" % (head, body, b64url(der_ecdsa_to_raw(der)))
        self.jwt = (time.time(), cfg["keyId"], token)
        return token

    async def send(self, title, body, sid=None):
        """One alert to every registered phone. Returns (sent, errors)."""
        cfg = self.config()
        if not cfg or not self.devices:
            return 0, []
        try:
            bearer = await self.bearer(cfg)
        except Exception as e:
            log("push: signing failed", repr(e))
            return 0, ["signing failed"]
        sent, errors = 0, []
        for token, dev in list(self.devices.items()):
            payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}, "host": dev.get("host")}
            headers = ["-H", "authorization: bearer " + bearer, "-H", "apns-topic: " + dev.get("topic", "com.kadaliao.steer"),
                       "-H", "apns-push-type: alert"]
            if sid:
                payload["aps"]["thread-id"] = sid
                payload["sid"] = sid
                headers += ["-H", "apns-collapse-id: " + sid[:64]]
            server = "api.sandbox.push.apple.com" if dev.get("env") == "sandbox" else "api.push.apple.com"
            args = [find_bin("curl") or "curl", "-sS", "--http2", "-o", "-", "-w", "\n%{http_code}"] + headers + \
                ["--data-binary", "@-", "https://%s/3/device/%s" % (server, token)]
            proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload, ensure_ascii=False).encode()), 20)
            except asyncio.TimeoutError:
                proc.kill()
                errors.append("timeout")
                continue
            reply, _, code = out.decode("utf-8", "replace").strip().rpartition("\n")
            if code == "200":
                sent += 1
                continue
            reason = ""
            try:
                reason = json.loads(reply).get("reason", "")
            except ValueError:
                reason = reply or err.decode("utf-8", "replace").strip()[:120]
            errors.append(reason or code)
            log("push failed", code, reason)
            if code == "410" or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                self.devices.pop(token, None)
                self.save()
        return sent, errors


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.watching = False
        self.open = set()
        self.closed = False

    def send(self, obj):
        if self.closed:
            return
        try:
            self.writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            self.closed = True


class Hub:
    def __init__(self):
        self.sessions = {}
        self.clients = set()
        self.codex = CodexBackend(self)
        self.dirty = set()
        self.flush_scheduled = False
        self.claude_info = self._load_cache()
        self.claude_cmds = {}     # cwd -> (time, commands)
        self.codex_skills = {}    # cwd -> (time, skills)
        self.probe_locks = {}
        self._claude_help = (0, "")
        self.caffeinate = None
        self.pusher = Pusher()
        self.agent_versions = {}
        self.stopping = asyncio.Event()

    # persistence -------------------------------------------------------
    def _load_cache(self):
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cache(self):
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump(self.claude_info, f, ensure_ascii=False)
        except Exception:
            pass

    def remember_claude_info(self, info, cwd=None):
        self.claude_info["models"] = info.get("models") or self.claude_info.get("models")
        commands = claude_command_list(info)
        if commands:
            self.claude_info["commands"] = commands
            if cwd:
                self.claude_cmds[cwd] = (time.time(), commands)
        self._save_cache()

    def remember_claude_commands(self, names):
        if not self.claude_info.get("commands"):
            self.claude_info["commands"] = [{"name": n, "description": "", "argumentHint": "", "kind": "skill"}
                                            for n in names]
            self._save_cache()

    # agent capabilities ------------------------------------------------
    async def claude_supports(self, flag):
        """Whether the installed claude lists `flag` in --help (checked hourly)."""
        checked, text = self._claude_help
        if time.time() - checked > 3600:
            text = ""
            claude = find_bin("claude")
            if claude:
                proc = await asyncio.create_subprocess_exec(
                    claude, "--help", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), 20)
                    text = out.decode("utf-8", "replace")
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            self._claude_help = (time.time(), text)
        return flag in text

    async def claude_probe(self, cwd):
        """Starts Claude just long enough to answer `initialize`: its commands,
        skills and models for this folder. No message is sent, so nothing is
        recorded and no usage is spent; hooks are switched off for the probe."""
        claude = find_bin("claude")
        if not claude:
            raise OpError("agent_missing", "claude not found on PATH")
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        proc = await asyncio.create_subprocess_exec(
            claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--settings", json.dumps({"disableAllHooks": True}),
            cwd=cwd if os.path.isdir(cwd) else HOME, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=LINE_LIMIT)

        async def answer():
            request = {"type": "control_request", "request_id": "probe", "request": {"subtype": "initialize"}}
            proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            while True:
                line = await proc.stdout.readline()
                if not line:
                    raise OpError("agent_failed", "Claude exited")
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if m.get("type") == "control_response":
                    resp = m.get("response") or {}
                    if resp.get("subtype") == "error":
                        raise OpError("agent_error", resp.get("error") or "error")
                    return resp.get("response") or {}

        try:
            info = await asyncio.wait_for(answer(), 30)
        except asyncio.TimeoutError:
            raise OpError("timeout", "Claude did not answer")
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
        self.remember_claude_info(info, cwd)
        return info

    async def codex_skill_list(self, cwd, force=False):
        cached = self.codex_skills.get(cwd)
        if cached and not force and time.time() - cached[0] < COMMANDS_TTL:
            return cached[1]
        r = await self.codex.request("skills/list", {"cwds": [cwd], "forceReload": bool(force)}, timeout=30)
        skills = []
        for entry in (r or {}).get("data") or []:
            for s in entry.get("skills") or []:
                if s.get("enabled") is False or not s.get("name"):
                    continue
                iface = s.get("interface") or {}
                skills.append({"name": s["name"], "path": s.get("path"),
                               "description": head(iface.get("shortDescription") or s.get("shortDescription")
                                                   or s.get("description") or "", 160)})
        self.codex_skills[cwd] = (time.time(), skills)
        return skills

    def load_sessions(self):
        os.makedirs(META_DIR, exist_ok=True)
        for path in glob.glob(os.path.join(META_DIR, "*.json")):
            try:
                with open(path) as f:
                    meta = json.load(f)
                self.add_session(meta)
            except Exception as e:
                log("bad session meta", path, e)

    def add_session(self, meta):
        cls = ClaudeSession if meta.get("agent") == "claude" else CodexSession
        s = cls(self, meta)
        self.sessions[s.sid] = s
        return s

    # fan-out -----------------------------------------------------------
    def to_watchers(self, sid, msg):
        for c in list(self.clients):
            if sid in c.open:
                c.send(msg)

    def session_changed(self, s, quiet=False, urgent=False):
        self.dirty.add(s.sid)
        if urgent:
            self._flush()
            return
        if not self.flush_scheduled:
            self.flush_scheduled = True
            asyncio.get_event_loop().call_later(0.6 if quiet else 0.15, self._flush)

    def _flush(self):
        self.flush_scheduled = False
        dirty, self.dirty = self.dirty, set()
        for sid in dirty:
            s = self.sessions.get(sid)
            if s is None:
                continue
            summary = s.summary()
            for c in list(self.clients):
                if c.watching or sid in c.open:
                    c.send({"ev": "session", "session": summary})

    # ops -----------------------------------------------------------------
    async def dispatch(self, client, req):
        op = req.get("op")
        rid = req.get("id")
        try:
            handler = getattr(self, "op_" + (op or "").replace(".", "_"), None)
            if handler is None:
                raise OpError("unknown_op", op or "")
            result = await handler(client, req)
            client.send({"id": rid, "ok": True, "result": result if result is not None else {}})
        except OpError as e:
            client.send({"id": rid, "ok": False, "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            log("op failed", op, repr(e))
            client.send({"id": rid, "ok": False, "error": {"code": "internal", "message": repr(e)}})

    def session(self, sid) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise OpError("unknown_session", sid or "")
        return s

    async def op_hello(self, client, req):
        return {
            "helper": VERSION,
            "proto": PROTO,
            "home": HOME,
            "host": socket.gethostname(),
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "launchd": os.environ.get("STEER_LAUNCHED_BY", ""),
            "agents": await self.agents(),
            "push": self.pusher.configured(),
        }

    async def op_push_register(self, client, req):
        self.pusher.register(req.get("token"), req.get("env"), req.get("host"), req.get("topic"))
        return {"configured": self.pusher.configured()}

    async def op_push_test(self, client, req):
        if not self.pusher.configured():
            raise OpError("push_not_configured", "这台电脑还没有 APNs 密钥")
        sent, errors = await self.pusher.send("Steer", "测试通知：%s 能给你推送了" % socket.gethostname())
        return {"sent": sent, "errors": errors}

    def notify(self, title, body, sid):
        if self.pusher.devices:
            asyncio.ensure_future(self.pusher.send(title, body, sid))

    async def agents(self):
        out = {}
        for name in ("claude", "codex"):
            path = find_bin(name)
            if not path:
                out[name] = None
                continue
            version = self.agent_versions.get(path)
            if version is None:
                try:
                    proc = await asyncio.create_subprocess_exec(path, "--version", stdout=asyncio.subprocess.PIPE,
                                                                stderr=asyncio.subprocess.DEVNULL)
                    data, _ = await asyncio.wait_for(proc.communicate(), 15)
                    version = data.decode("utf-8", "replace").strip().splitlines()[0] if data else ""
                except Exception:
                    version = ""
                self.agent_versions[path] = version
            out[name] = {"path": path, "version": version}
        return out

    async def op_watch(self, client, req):
        client.watching = True
        return await self.op_sessions_list(client, req)

    async def op_sessions_list(self, client, req):
        loop = asyncio.get_event_loop()
        out = {}
        for s in self.sessions.values():
            if not s.meta.get("archived"):
                out[s.sid] = s.summary()
        hidden = {s.sid for s in self.sessions.values() if s.meta.get("archived")}
        try:
            history = await loop.run_in_executor(None, claude_history)
        except Exception as e:
            log("claude history failed", repr(e))
            history = []
        open_now = claude_open_sessions()
        for h in history:
            if h["sid"] in out or h["sid"] in hidden:
                continue
            entry = open_now.get(h["sid"])
            out[h["sid"]] = {"sid": h["sid"], "agent": "claude", "cwd": h["cwd"], "title": h["title"],
                             "state": "running" if entry and entry["status"] not in TERMINAL_IDLE else "idle",
                             "needs": None, "lastText": h["lastText"], "activity": None, "updatedAt": h["updatedAt"],
                             "runningSince": None, "model": None, "mode": None, "source": "desktop", "live": False,
                             "error": None, "openInTerminal": bool(entry)}
        if find_bin("codex"):
            try:
                r = await self.codex.request("thread/list", {"limit": 50, "sortKey": "updated_at",
                                                             "sourceKinds": ["cli", "vscode", "exec", "appServer"]},
                                             timeout=20)
                for t in (r or {}).get("data") or []:
                    sid = t.get("id")
                    if not sid or sid in out or sid in hidden or t.get("ephemeral"):
                        continue
                    if time.time() - (t.get("updatedAt") or 0) > HISTORY_DAYS * 86400:
                        continue
                    out[sid] = {"sid": sid, "agent": "codex", "cwd": t.get("cwd") or "", "title": t.get("name") or one_line(t.get("preview") or "", 60),
                                "state": "idle", "needs": None, "lastText": "", "activity": None,
                                "updatedAt": (t.get("updatedAt") or 0) * 1000, "runningSince": None,
                                "model": t.get("model"), "mode": None, "source": "desktop", "live": False, "error": None}
            except OpError as e:
                log("codex thread/list failed", e.message)
        sessions = sorted(out.values(), key=lambda s: -(s.get("updatedAt") or 0))
        return {"sessions": sessions}

    async def op_session_open(self, client, req):
        sid = req.get("sid")
        s = self.sessions.get(sid)
        if s is None:
            agent = req.get("agent")
            if agent not in ("claude", "codex"):
                raise OpError("unknown_session", sid or "")
            meta = {"sid": sid, "agent": agent, "cwd": req.get("cwd") or HOME, "title": req.get("title") or "",
                    "source": "desktop", "started": True, "persist": False}
            s = self.add_session(meta)
        await s.load()
        client.open.add(sid)
        return s.snapshot()

    async def op_session_close(self, client, req):
        client.open.discard(req.get("sid"))
        return {}

    async def op_session_create(self, client, req):
        agent = req.get("agent")
        cwd = os.path.expanduser(req.get("cwd") or "~")
        if not os.path.isdir(cwd):
            raise OpError("cwd_missing", "这台电脑上没有这个文件夹：" + cwd)
        meta = {"agent": agent, "cwd": cwd, "title": "", "source": "steer", "persist": True,
                "createdAt": now_ms(), "mode": req.get("mode"), "model": req.get("model")}
        if agent == "claude":
            meta["sid"] = str(uuid.uuid4())
            s = self.add_session(meta)
        elif agent == "codex":
            meta["sid"] = "pending-" + uuid.uuid4().hex
            s = CodexSession(self, meta)
            meta["sid"] = await s.create_thread()
            meta["started"] = True
            self.sessions[s.sid] = s
        else:
            raise OpError("bad_agent", str(agent))
        s.loaded = True
        client.open.add(s.sid)
        s.save()
        result = {"session": s.summary()}
        if req.get("text") or req.get("images"):
            sent = await s.send(req.get("text") or "", req.get("images") or [])
            result["sent"] = sent
            result["session"] = s.summary()
        return result

    async def op_session_send(self, client, req):
        s = self.session(req.get("sid"))
        return await s.send(req.get("text") or "", req.get("images") or [])

    async def op_session_interrupt(self, client, req):
        return await self.session(req.get("sid")).interrupt()

    async def op_session_respond(self, client, req):
        s = self.session(req.get("sid"))
        return await s.respond(req.get("rid"), req.get("decision") or "allow", req.get("message") or "",
                               req.get("answers") or {})

    async def op_session_setMode(self, client, req):
        s = self.session(req.get("sid"))
        await s.set_mode(req.get("mode"))
        return s.summary()

    async def op_session_setModel(self, client, req):
        s = self.session(req.get("sid"))
        await s.set_model(req.get("model"))
        return s.summary()

    async def op_session_rename(self, client, req):
        s = self.session(req.get("sid"))
        title = one_line(req.get("title") or "", 120)
        s.meta["title"] = title
        s.meta["titleLocked"] = True
        s.adopt()
        if isinstance(s, CodexSession):
            await s.rename(title)
        s.save()
        s.touch()
        return s.summary()

    async def op_session_archive(self, client, req):
        sid = req.get("sid")
        s = self.sessions.get(sid)
        if s is None:
            meta = {"sid": sid, "agent": req.get("agent") or "claude", "cwd": req.get("cwd") or HOME,
                    "source": "desktop", "started": True, "persist": True}
            s = self.add_session(meta)
        s.meta["archived"] = bool(req.get("archived", True))
        s.adopt()
        s.save()
        if s.meta["archived"]:
            await s.shutdown()
        s.touch()
        return {}

    async def op_files_search(self, client, req):
        cwd = os.path.expanduser(req.get("cwd") or "~")
        loop = asyncio.get_event_loop()
        files = await loop.run_in_executor(None, search_files, cwd, req.get("query") or "", int(req.get("limit") or 40))
        return {"files": files}

    async def op_dirs_list(self, client, req):
        return list_dirs(req.get("path") or "~")

    async def op_dirs_create(self, client, req):
        return make_dir(req.get("path") or "~", req.get("name") or "")

    async def op_projects_recent(self, client, req):
        listing = await self.op_sessions_list(client, req)
        seen = {}
        for s in listing["sessions"]:
            cwd = s.get("cwd")
            if not cwd or not os.path.isdir(cwd):
                continue
            entry = seen.get(cwd)
            if entry is None:
                seen[cwd] = {"path": cwd, "name": os.path.basename(cwd.rstrip("/")) or cwd,
                             "updatedAt": s.get("updatedAt") or 0, "agents": [s["agent"]],
                             "git": os.path.isdir(os.path.join(cwd, ".git"))}
            elif s["agent"] not in entry["agents"]:
                entry["agents"].append(s["agent"])
        projects = sorted(seen.values(), key=lambda p: -p["updatedAt"])
        return {"projects": projects[:40]}

    async def op_agent_info(self, client, req):
        agent = req.get("agent")
        if agent == "claude":
            if not self.claude_info.get("models"):
                try:
                    await self.claude_probe(req.get("cwd") or HOME)
                except OpError:
                    pass
            models = [{"id": m.get("value"), "name": m.get("displayName"), "description": m.get("description")}
                      for m in self.claude_info.get("models") or []]
            if not models:
                models = [{"id": v, "name": n, "description": ""} for v, n in
                          (("default", "Default"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"))]
            return {"models": models, "commands": self.claude_info.get("commands") or [],
                    "modes": ["default", "acceptEdits", "auto", "plan", "bypassPermissions"]}
        if agent == "codex":
            if self.codex.models is None:
                try:
                    r = await self.codex.request("model/list", {}, timeout=20)
                    self.codex.models = [{"id": m.get("id"), "name": m.get("displayName") or m.get("id"),
                                          "description": m.get("description") or "", "default": m.get("isDefault")}
                                         for m in (r or {}).get("data") or []]
                except OpError:
                    return {"models": [], "commands": [], "modes": list(CODEX_MODES)}
            return {"models": self.codex.models, "commands": [], "modes": ["untrusted", "auto", "readOnly", "full"]}
        raise OpError("bad_agent", str(agent))

    async def op_agent_commands(self, client, req):
        agent = req.get("agent")
        cwd = req.get("cwd") or HOME
        reload = bool(req.get("reload"))
        if agent == "claude":
            lock = self.probe_locks.setdefault(cwd, asyncio.Lock())
            async with lock:
                cached = self.claude_cmds.get(cwd)
                if cached and not reload and time.time() - cached[0] < COMMANDS_TTL:
                    return {"commands": cached[1]}
                try:
                    await self.claude_probe(cwd)
                except OpError:
                    if not self.claude_info.get("commands"):
                        raise
                    return {"commands": self.claude_info["commands"]}
                return {"commands": self.claude_cmds[cwd][1]}
        if agent == "codex":
            skills = await self.codex_skill_list(cwd, force=reload)
            return {"commands": [{"name": s["name"], "description": s["description"], "argumentHint": "",
                                  "kind": "skill"} for s in skills]}
        raise OpError("bad_agent", str(agent))

    async def op_ping(self, client, req):
        return {"t": now_ms()}

    async def op_shutdown(self, client, req):
        busy = [s.sid for s in self.sessions.values() if s.holds_helper()]
        if busy and not req.get("force"):
            raise OpError("busy", "sessions are running: " + ", ".join(busy))
        asyncio.get_event_loop().call_later(0.2, self.stopping.set)
        return {}

    # lifecycle -----------------------------------------------------------
    async def handle_client(self, reader, writer):
        client = Client(writer)
        self.clients.add(client)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except Exception:
                    continue
                asyncio.ensure_future(self.dispatch(client, req))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            client.closed = True
            self.clients.discard(client)
            try:
                writer.close()
            except Exception:
                pass

    async def watch_terminals(self):
        """Once a second: follow Claude sessions a terminal has open (state and
        new transcript lines), and keep the computer awake while work runs."""
        while not self.stopping.is_set():
            await asyncio.sleep(1)
            try:
                open_now = claude_open_sessions(max_age=0.9)
                for sid, entry in open_now.items():
                    if sid not in self.sessions:
                        self.adopt_terminal_session(sid, entry)
                for sess in list(self.sessions.values()):
                    if isinstance(sess, ClaudeSession):
                        sess.follow_terminal(open_now.get(sess.native_id))
            except Exception as e:
                log("terminal watch", repr(e))
            self.update_awake()

    def adopt_terminal_session(self, sid, entry):
        path = claude_transcript_path(sid, entry.get("cwd"))
        if not path:
            return  # nothing said yet
        try:
            info = claude_history_entry(path) or {}
        except Exception:
            info = {}
        meta = {"sid": sid, "agent": "claude", "cwd": entry.get("cwd") or info.get("cwd") or HOME,
                "title": info.get("title") or "", "lastText": info.get("lastText") or "", "source": "desktop",
                "started": True, "persist": False}
        self.add_session(meta)

    def update_awake(self):
        """`caffeinate` while any session runs or waits for an answer, so the
        computer does not fall asleep under it."""
        if sys.platform != "darwin":
            return
        busy = any(sess.state == "running" or sess.pending for sess in self.sessions.values())
        if self.caffeinate is not None and self.caffeinate.poll() is not None:
            self.caffeinate = None
        if busy and self.caffeinate is None:
            path = shutil.which("caffeinate") or "/usr/bin/caffeinate"
            if os.path.exists(path):
                self.caffeinate = subprocess.Popen([path, "-i", "-s", "-w", str(os.getpid())],
                                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL)
                log("keeping the computer awake")
        elif not busy and self.caffeinate is not None:
            self.caffeinate.terminate()
            try:
                self.caffeinate.wait(2)
            except subprocess.TimeoutExpired:
                pass
            self.caffeinate = None
            log("the computer may sleep again")

    async def janitor(self):
        while not self.stopping.is_set():
            await asyncio.sleep(60)
            for s in list(self.sessions.values()):
                if isinstance(s, ClaudeSession):
                    s.reap_if_idle()
            for c in list(self.clients):
                try:
                    await c.writer.drain()
                except Exception:
                    pass

    async def shutdown_all(self):
        for s in list(self.sessions.values()):
            try:
                await s.shutdown()
            except Exception:
                pass
        await self.codex.close()


async def serve_main():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SOCK):
        if can_connect():
            log("another daemon is running; exiting")
            return
        os.remove(SOCK)
    prune_uploads()
    hub = Hub()
    hub.load_sessions()
    server = await asyncio.start_unix_server(hub.handle_client, path=SOCK, limit=LINE_LIMIT)
    os.chmod(SOCK, 0o600)
    log("steerd", VERSION, "listening on", SOCK, "pid", os.getpid())
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, hub.stopping.set)
        except NotImplementedError:
            pass
    janitor = asyncio.ensure_future(hub.janitor())
    watcher = asyncio.ensure_future(hub.watch_terminals())
    await hub.stopping.wait()
    log("stopping")
    janitor.cancel()
    watcher.cancel()
    if hub.caffeinate is not None:
        hub.caffeinate.terminate()
    server.close()
    await hub.shutdown_all()
    try:
        os.remove(SOCK)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# attach / daemon management
# ---------------------------------------------------------------------------

def can_connect() -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(SOCK)
        return True
    except OSError:
        return False
    finally:
        s.close()


def launch_agent_path():
    return os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")


def stable_path(value: str) -> str:
    """PATH without per-shell temporary entries (fnm and friends), deduplicated."""
    seen = []
    for entry in (value or "").split(":"):
        if not entry or "fnm_multishells" in entry or entry in seen:
            continue
        seen.append(entry)
    return ":".join(seen)


def launchctl(*args):
    return subprocess.run(["launchctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def start_with_launchd() -> bool:
    """Run the daemon in the logged-in user's GUI session so Claude can read its Keychain login."""
    env = {k: os.environ[k] for k in ("HOME", "SHELL", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")
           if os.environ.get(k)}
    env["PATH"] = stable_path(os.environ.get("PATH", "")) or "/usr/bin:/bin:/usr/sbin:/sbin"
    env["STEER_LAUNCHED_BY"] = "launchd"
    if "STEER_HOME" in os.environ:
        env["STEER_HOME"] = os.environ["STEER_HOME"]
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, os.path.abspath(__file__), "serve"],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        "WorkingDirectory": HOME,
    }
    path = launch_agent_path()
    data = plistlib.dumps(plist)
    try:
        with open(path, "rb") as f:
            old_data = f.read()
        old = plistlib.loads(old_data)
    except Exception:
        old_data, old = None, None
    # Only a different interpreter, script or data dir needs a reload; a
    # changed PATH is picked up the next time the agent loads anyway.
    critical_changed = old is None or old.get("ProgramArguments") != plist["ProgramArguments"] or \
        (old.get("EnvironmentVariables") or {}).get("STEER_HOME") != env.get("STEER_HOME")
    if old_data != data:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    domain = "gui/%d" % os.getuid()
    target = domain + "/" + LABEL
    loaded = launchctl("print", target).returncode == 0
    if loaded and critical_changed:
        launchctl("bootout", target)
        for _ in range(50):
            if launchctl("print", target).returncode != 0:
                break
            time.sleep(0.1)
        loaded = False
    if loaded:
        launchctl("kickstart", target)
        return True
    for attempt in range(5):
        r = launchctl("bootstrap", domain, path)
        if r.returncode == 0 or launchctl("print", target).returncode == 0:
            launchctl("kickstart", target)
            return True
        time.sleep(0.4 * (attempt + 1))
    sys.stderr.write("steerd: launchctl bootstrap failed: %s\n" % r.stdout.decode("utf-8", "replace").strip())
    return False


def spawn_detached():
    log_file = open(LOG_PATH, "ab")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], stdin=subprocess.DEVNULL,
                     stdout=log_file, stderr=log_file, cwd=HOME, start_new_session=True,
                     env=dict(os.environ, STEER_LAUNCHED_BY="detached"))


def ensure_daemon():
    if can_connect():
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    started = False
    if sys.platform == "darwin" and not os.environ.get("STEER_NO_LAUNCHD"):
        started = start_with_launchd()
    if not started:
        spawn_detached()
    deadline = time.time() + 15
    while time.time() < deadline:
        if can_connect():
            return
        time.sleep(0.1)
    sys.stderr.write("steerd: daemon did not start; see %s\n" % LOG_PATH)
    sys.exit(3)


def cmd_attach():
    ensure_daemon()
    import select
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    sock_fd = s.fileno()
    stdin_open = True
    while True:
        fds = [sock_fd] + ([stdin_fd] if stdin_open else [])
        readable, _, _ = select.select(fds, [], [])
        if stdin_fd in readable:
            data = os.read(stdin_fd, 65536)
            if not data:
                stdin_open = False
                try:
                    s.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                break
            s.sendall(data)
        if sock_fd in readable:
            data = s.recv(65536)
            if not data:
                break
            view = memoryview(data)
            while view:
                n = os.write(stdout_fd, view)
                view = view[n:]
    s.close()


def cmd_stop(force=False):
    if not can_connect():
        print("not running")
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall((json.dumps({"id": 1, "op": "shutdown", "force": force}) + "\n").encode())
    reply = s.makefile().readline().strip()
    s.close()
    print(reply)
    if '"ok": true' in reply:
        # Wait until the old daemon is really gone so a new attach starts a fresh one.
        deadline = time.time() + 10
        while time.time() < deadline and can_connect():
            time.sleep(0.1)


def cmd_push_key(args):
    """steerd.py push-key AuthKey_XXXX.p8 KEY_ID TEAM_ID: install the APNs key."""
    if len(args) != 3:
        print("usage: steerd.py push-key AuthKey_<id>.p8 <key id> <team id>")
        sys.exit(2)
    path, key_id, team_id = args
    with open(path, "rb") as f:
        key = f.read()
    if b"PRIVATE KEY" not in key:
        print("not a .p8 private key")
        sys.exit(1)
    os.makedirs(APNS_DIR, mode=0o700, exist_ok=True)
    os.chmod(APNS_DIR, 0o700)
    target = os.path.join(APNS_DIR, "AuthKey_%s.p8" % key_id)
    with open(target, "wb") as f:
        f.write(key)
    os.chmod(target, 0o600)
    with open(os.path.join(APNS_DIR, "apns.json"), "w") as f:
        json.dump({"keyId": key_id, "teamId": team_id}, f)
    print("APNs key installed:", key_id, "team", team_id)


def cmd_doctor():
    print("version", VERSION)
    print("python", sys.executable, sys.version.split()[0])
    print("data", DATA_DIR)
    print("socket", SOCK, "up" if can_connect() else "down")
    for name in ("claude", "codex"):
        print(name, find_bin(name) or "missing")
    if sys.platform == "darwin":
        print("launch agent", launch_agent_path(), "present" if os.path.exists(launch_agent_path()) else "absent")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "attach"
    if cmd == "attach":
        cmd_attach()
    elif cmd == "serve":
        asyncio.run(serve_main()) if hasattr(asyncio, "run") else asyncio.get_event_loop().run_until_complete(serve_main())
    elif cmd == "stop":
        cmd_stop(force="--force" in argv)
    elif cmd == "version":
        print(VERSION)
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "push-key":
        cmd_push_key(argv[2:])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)
