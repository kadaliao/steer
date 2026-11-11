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
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

VERSION = "0.1.2"
PROTO = 1

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("STEER_HOME") or os.path.join(HOME, ".local", "share", "steer")
SOCK = os.environ.get("STEER_SOCK") or os.path.join(DATA_DIR, "steerd.sock")
LOG_PATH = os.path.join(DATA_DIR, "steerd.log")
META_DIR = os.path.join(DATA_DIR, "sessions")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CACHE_PATH = os.path.join(DATA_DIR, "agent-info.json")
LABEL = "com.kadaliao.steer.helper"
LINE_LIMIT = 256 * 1024 * 1024

OUTPUT_TAIL = 4000
DIFF_LIMIT = 40000
SNAPSHOT_ITEMS = 400
CLAUDE_IDLE_EXIT = 30 * 60
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

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.items = []
        self.index = {}
        self.title = None
        self.text_counts = {}

    def put(self, item):
        if item["id"] in self.index:
            self.items[self.index[item["id"]]].update(item)
        else:
            self.index[item["id"]] = len(self.items)
            self.items.append(item)

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
        }

    def is_live(self) -> bool:
        return False

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

    @property
    def native_id(self):
        return self.meta.get("nativeId") or self.sid

    def is_live(self):
        return self.proc is not None and self.proc.returncode is None

    async def load(self):
        if self.loaded:
            return
        self.loaded = True
        path = claude_transcript_path(self.native_id, self.cwd)
        if not path:
            return
        loop = asyncio.get_event_loop()
        transcript = await loop.run_in_executor(None, self._read_transcript, path)
        live_items = self.items
        self.items, self.index = [], {}
        for item in transcript.items:
            self.put(item, emit=False)
        for item in live_items:
            if item["id"] not in self.index:
                self.put(item, emit=False)
        if transcript.title and not self.meta.get("titleLocked"):
            self.meta["title"] = transcript.title
        for item in reversed(self.items):
            if item.get("kind") == "assistant" and (item.get("text") or "").strip():
                self.last_text = one_line(item["text"], 160)
                break

    def _read_transcript(self, path):
        transcript = ClaudeTranscript(self.cwd)
        with open(path, "rb") as f:
            for m in iter_json_lines(f.read()):
                transcript.feed(m)
        return transcript

    async def ensure_process(self):
        if self.is_live():
            return
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
        if not os.path.isdir(self.cwd):
            raise OpError("cwd_missing", self.cwd)
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
            info = await self.control({"subtype": "initialize"}, timeout=90)
        except OpError:
            raise OpError("agent_failed", "\n".join(self.stderr_tail[-5:]) or "Claude did not start")
        if isinstance(info, dict):
            self.hub.remember_claude_info(info)
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
        else:
            self.set_state("idle")

    async def write(self, obj):
        if not self.is_live():
            raise OpError("agent_not_running")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

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
        self.set_state("idle")

    # actions ---------------------------------------------------------
    async def send(self, text, images):
        await self.load()
        busy = self.state == "running" or bool(self.pending)
        item = {"id": new_id("u"), "kind": "user", "text": text, "images": len(images or []), "queued": busy}
        self.put(item)
        if busy:
            self.queued.append(item["id"])
        try:
            await self.ensure_process()
            if images:
                content = [{"type": "image", "source": {"type": "base64", "media_type": img.get("mime", "image/png"),
                                                        "data": img.get("data", "")}} for img in images]
                if text:
                    content.append({"type": "text", "text": text})
            else:
                content = text
            await self.write({"type": "user", "message": {"role": "user", "content": content}})
        except OpError as e:
            item["failed"] = True
            self.emit_item(item)
            self.notice("send_failed", e.message)
            raise
        if not self.meta.get("started"):
            self.meta["started"] = True
        if not self.meta.get("title"):
            self.meta["title"] = one_line(text, 60)
        self.adopt()
        self.save()
        if not busy:
            self.set_state("running")
        return {"id": item["id"]}

    async def interrupt(self):
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


class CodexBackend:
    def __init__(self, hub):
        self.hub = hub
        self.proc = None
        self.next_id = 0
        self.waiters = {}
        self.generation = 0
        self.lock = asyncio.Lock()
        self.models = None

    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self):
        async with self.lock:
            if self.alive():
                return
            codex = find_bin("codex")
            if not codex:
                raise OpError("agent_missing", "codex not found on PATH")
            log("codex app-server start")
            self.proc = await asyncio.create_subprocess_exec(
                codex, "app-server", cwd=HOME, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
            self.generation += 1
            proc = self.proc
            asyncio.ensure_future(self._read(proc))
            asyncio.ensure_future(self._drain_stderr(proc))
            await self._request("initialize", {"clientInfo": {"name": "steer", "title": "Steer", "version": VERSION}})
            await self._send({"method": "initialized"})

    async def _drain_stderr(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break

    async def _send(self, obj):
        if not self.alive():
            raise OpError("agent_not_running")
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
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
                try:
                    self.on_message(m)
                except Exception as e:
                    log("codex message error", repr(e))
        finally:
            await proc.wait()
            log("codex app-server exit", proc.returncode)
            if proc is self.proc:
                self.proc = None
                for fut in self.waiters.values():
                    if not fut.done():
                        fut.set_exception(OpError("agent_exited"))
                self.waiters = {}
                for s in self.hub.sessions.values():
                    if isinstance(s, CodexSession):
                        s.on_backend_exit()

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
        if self.is_live():
            return
        await self.hub.codex.ensure()
        approval, _ = self.policy()
        params = {"threadId": self.sid, "excludeTurns": True, "approvalPolicy": approval,
                  "sandbox": CODEX_SANDBOX_MODE.get(self.meta.get("mode") or "auto", "workspace-write")}
        if self.meta.get("model"):
            params["model"] = self.meta["model"]
        await self.hub.codex.request("thread/resume", params)
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


# ---------------------------------------------------------------------------
# Hub: clients, sessions, ops
# ---------------------------------------------------------------------------

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

    def remember_claude_info(self, info):
        self.claude_info["models"] = info.get("models") or self.claude_info.get("models")
        commands = []
        for c in info.get("commands") or []:
            commands.append({"name": c.get("name"), "description": head(c.get("description") or "", 160),
                             "argumentHint": c.get("argumentHint") or ""})
        if commands:
            self.claude_info["commands"] = commands
        self._save_cache()

    def remember_claude_commands(self, names):
        if not self.claude_info.get("commands"):
            self.claude_info["commands"] = [{"name": n, "description": "", "argumentHint": ""} for n in names]
            self._save_cache()

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
        }

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
        for h in history:
            if h["sid"] in out or h["sid"] in hidden:
                continue
            out[h["sid"]] = {"sid": h["sid"], "agent": "claude", "cwd": h["cwd"], "title": h["title"], "state": "idle",
                             "needs": None, "lastText": h["lastText"], "activity": None, "updatedAt": h["updatedAt"],
                             "runningSince": None, "model": None, "mode": None, "source": "desktop", "live": False,
                             "error": None}
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
            raise OpError("cwd_missing", cwd)
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

    async def op_ping(self, client, req):
        return {"t": now_ms()}

    async def op_shutdown(self, client, req):
        busy = [s.sid for s in self.sessions.values() if s.state == "running" or s.pending]
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
        if self.codex.alive():
            try:
                self.codex.proc.stdin.close()
                await asyncio.wait_for(self.codex.proc.wait(), 5)
            except Exception:
                self.codex.proc.terminate()


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
    await hub.stopping.wait()
    log("stopping")
    janitor.cancel()
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
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)
