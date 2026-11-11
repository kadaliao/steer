"""Unit tests for how steerd turns Claude / Codex protocol messages into timeline items.

Messages mirror shapes recorded from claude 2.1.322 and codex-cli 0.167.0.
Run: python3 -m unittest discover -s helper/tests -p 'test_*.py'
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import steerd  # noqa: E402


class FakeHub:
    def __init__(self):
        self.sent = []
        self.sessions = {}

    def to_watchers(self, sid, msg):
        self.sent.append(msg)

    def session_changed(self, s, quiet=False, urgent=False):
        pass

    def remember_claude_info(self, info, cwd=None):
        pass

    def remember_claude_commands(self, names):
        pass


def claude_session():
    hub = FakeHub()
    s = steerd.ClaudeSession(hub, {"sid": "s1", "agent": "claude", "cwd": "/work/proj"})
    s.written = []

    async def write(obj):
        s.written.append(obj)
    s.write = write
    return s


def stream(event):
    return {"type": "stream_event", "event": event}


class ClaudeStreamTests(unittest.TestCase):
    def test_edit_permission_then_result_and_reply(self):
        s = claude_session()
        s.on_message(stream({"type": "message_start", "message": {"id": "m1"}}))
        s.on_message(stream({"type": "content_block_start", "index": 0,
                             "content_block": {"type": "tool_use", "id": "tu1", "name": "Edit", "input": {}}}))
        edit_input = {"file_path": "/work/proj/calc.py", "old_string": "a\n", "new_string": "a\nb\n"}
        s.on_message({"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Edit", "input": edit_input}]}})
        s.on_message({"type": "control_request", "request_id": "r1", "request": {
            "subtype": "can_use_tool", "tool_name": "Edit", "input": edit_input,
            "permission_suggestions": [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]}})

        self.assertEqual(s.summary()["state"], "waiting")
        self.assertEqual(s.summary()["needs"], "permission")
        view = s.pending["r1"]["view"]
        self.assertEqual((view["category"], view["target"], view["add"], view["del"]), ("edit", "calc.py", 1, 0))

        asyncio.run(s.respond("r1", "allowSession", "", {}))
        reply = s.written[-1]["response"]
        self.assertEqual(reply["request_id"], "r1")
        self.assertEqual(reply["response"]["behavior"], "allow")
        self.assertEqual(reply["response"]["updatedPermissions"][0]["destination"], "session")
        self.assertEqual(s.pending, {})

        s.on_message({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "updated", "is_error": False}]},
            "tool_use_result": {"structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 2, "lines": [" a", "+b"]}]}})
        tool = s.get("tu1")
        self.assertEqual((tool["status"], tool["add"], tool["del"]), ("ok", 1, 0))

        s.on_message(stream({"type": "message_start", "message": {"id": "m2"}}))
        s.on_message(stream({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        s.on_message(stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Added "}}))
        s.on_message(stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "b."}}))
        self.assertEqual(s.get("m2:t0")["text"], "Added b.")
        s.on_message({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Added b."}]}})
        self.assertFalse(s.get("m2:t0")["streaming"])

        s.on_message({"type": "result", "subtype": "success", "duration_ms": 4200, "is_error": False})
        self.assertEqual(s.items[-1]["kind"], "turn")
        self.assertEqual(s.items[-1]["status"], "completed")
        self.assertEqual(s.state, "idle")
        deltas = [m for m in s.hub.sent if m.get("ev") == "delta"]
        self.assertEqual([d["itemId"] for d in deltas], ["m2:t0", "m2:t0"])

    def test_question_answers_are_keyed_by_question_text(self):
        s = claude_session()
        questions = [{"question": "Tabs or spaces?", "header": "Indent", "multiSelect": False,
                      "options": [{"label": "Tabs", "description": ""}, {"label": "Spaces", "description": ""}]}]
        s.on_message({"type": "control_request", "request_id": "q1", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {"questions": questions}}})
        view = s.pending["q1"]["view"]
        self.assertEqual(view["kind"], "question")
        qid = view["questions"][0]["id"]
        asyncio.run(s.respond("q1", "allow", "", {qid: "Spaces"}))
        updated = s.written[-1]["response"]["response"]["updatedInput"]
        self.assertEqual(updated["answers"], {"Tabs or spaces?": "Spaces"})

    def test_plain_deny_stops_the_turn_and_a_note_does_not(self):
        s = claude_session()
        for rid in ("a", "b"):
            s.on_message({"type": "control_request", "request_id": rid, "request": {
                "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf build"}}})
        asyncio.run(s.respond("a", "deny", "", {}))
        self.assertTrue(s.written[-1]["response"]["response"]["interrupt"])
        asyncio.run(s.respond("b", "deny", "use git clean instead", {}))
        resp = s.written[-1]["response"]["response"]
        self.assertEqual(resp["message"], "use git clean instead")
        self.assertNotIn("interrupt", resp)

    def test_plan_approval_can_switch_to_accept_edits(self):
        s = claude_session()
        s.on_message({"type": "control_request", "request_id": "p", "request": {
            "subtype": "can_use_tool", "tool_name": "ExitPlanMode", "input": {"plan": "1. do it"}}})
        self.assertEqual(s.pending["p"]["view"], {"kind": "plan", "plan": "1. do it", "rid": "p",
                                                  "ts": s.pending["p"]["view"]["ts"]})
        asyncio.run(s.respond("p", "allowSession", "", {}))
        perms = s.written[-1]["response"]["response"]["updatedPermissions"]
        self.assertEqual(perms[0]["mode"], "acceptEdits")

    def test_subagent_messages_stay_out_of_the_timeline(self):
        s = claude_session()
        s.on_message({"type": "assistant", "parent_tool_use_id": "task1",
                      "message": {"id": "x", "content": [{"type": "text", "text": "inner"}]}})
        self.assertEqual(s.items, [])

    def test_todo_write_becomes_a_plan(self):
        s = claude_session()
        s.on_message({"type": "assistant", "message": {"id": "m", "content": [{"type": "tool_use", "id": "t",
                      "name": "TodoWrite", "input": {"todos": [
                          {"content": "Write tests", "status": "completed"},
                          {"content": "Fix bug", "status": "in_progress"}]}}]}})
        self.assertEqual(s.items[0]["kind"], "plan")
        self.assertEqual([st["status"] for st in s.items[0]["steps"]], ["completed", "inProgress"])


class TranscriptTests(unittest.TestCase):
    def test_parses_messages_tools_and_title(self):
        t = steerd.ClaudeTranscript("/work/proj")
        lines = [
            {"type": "user", "uuid": "u1", "message": {"content": "fix calc"}, "timestamp": "2026-11-11T10:31:56.534Z"},
            {"type": "user", "uuid": "meta", "isMeta": True, "message": {"content": "<system-reminder>x"}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "tool_use", "id": "tu", "name": "Bash", "input": {"command": "pytest"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu",
                                                      "content": "1 failed", "is_error": True}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "One test fails."}]}},
            {"type": "assistant", "isSidechain": True, "message": {"id": "m3", "content": [{"type": "text", "text": "x"}]}},
            {"type": "ai-title", "aiTitle": "Fix calc"},
        ]
        for line in lines:
            t.feed(line)
        self.assertEqual([i["kind"] for i in t.items], ["user", "tool", "assistant"])
        self.assertEqual(t.items[1]["status"], "error")
        self.assertEqual(t.items[1]["target"], "pytest")
        self.assertIsNotNone(t.items[0]["ts"])
        self.assertEqual(t.title, "Fix calc")

    def test_slash_command_lines_show_the_command(self):
        text = steerd.clean_user_text("<command-name>/compact</command-name>\n<command-args>keep tests</command-args>")
        self.assertEqual(text, "/compact keep tests")


class CodexItemTests(unittest.TestCase):
    def test_read_only_commands_are_reads(self):
        item = steerd.codex_item({"type": "commandExecution", "id": "e1",
                                  "command": "/bin/zsh -lc \"ls && sed -n '1,200p' calc.py\"",
                                  "commandActions": [{"type": "listFiles", "command": "ls", "path": None},
                                                     {"type": "read", "command": "sed", "path": "/work/proj/calc.py"}],
                                  "status": "completed", "exitCode": 0, "aggregatedOutput": "calc.py\n"},
                                 "/work/proj", {})
        self.assertEqual(item["category"], "read")
        self.assertEqual(item["command"], "ls && sed -n '1,200p' calc.py")
        self.assertEqual(item["status"], "ok")

    def test_failed_command_and_file_change(self):
        cmd = steerd.codex_item({"type": "commandExecution", "id": "e2", "command": "npm test", "commandActions": [],
                                 "status": "completed", "exitCode": 1}, "/w", {})
        self.assertEqual(cmd["status"], "error")
        change = steerd.codex_item({"type": "fileChange", "id": "f", "status": "completed", "changes": [
            {"path": "/w/calc.py", "kind": {"type": "update"}, "diff": "@@ -5 +5,4 @@\n     return a - b\n+\n+def mul(a, b):\n+    return a * b\n"}]},
            "/w", {})
        self.assertEqual((change["category"], change["target"], change["add"], change["del"]), ("edit", "calc.py", 3, 0))

    def test_user_message_keeps_the_client_id(self):
        item = steerd.codex_item({"type": "userMessage", "id": "server-id", "clientId": "u-123",
                                  "content": [{"type": "text", "text": "hi"}]}, "/w", {})
        self.assertEqual((item["id"], item["text"]), ("u-123", "hi"))

    def test_reasoning_is_hidden(self):
        self.assertIsNone(steerd.codex_item({"type": "reasoning", "id": "r"}, "/w", {}))


class CodexSessionTests(unittest.TestCase):
    def test_turn_lifecycle_and_approval(self):
        hub = FakeHub()
        hub.codex = type("Backend", (), {"alive": lambda self: True, "generation": 1})()
        s = steerd.CodexSession(hub, {"sid": "th", "agent": "codex", "cwd": "/w"})
        s.on_notification("turn/started", {"threadId": "th", "turn": {"id": "turn1"}})
        self.assertEqual(s.state, "running")
        s.on_notification("item/started", {"item": {"type": "fileChange", "id": "f1", "status": "inProgress",
                                                    "changes": [{"path": "/w/a.py", "kind": {"type": "add"}, "diff": "x\n"}]}})
        s.on_server_request({"id": 7, "method": "item/fileChange/requestApproval",
                             "params": {"threadId": "th", "turnId": "turn1", "itemId": "f1"}})
        view = s.pending["x7"]["view"]
        self.assertEqual((view["category"], view["target"], view["add"]), ("edit", "a.py", 1))
        s.on_notification("serverRequest/resolved", {"threadId": "th", "requestId": 7})
        self.assertEqual(s.pending, {})
        s.on_notification("item/agentMessage/delta", {"itemId": "m", "delta": "Do"})
        s.on_notification("item/agentMessage/delta", {"itemId": "m", "delta": "ne"})
        self.assertEqual(s.get("m")["text"], "Done")
        s.on_notification("turn/completed", {"turn": {"id": "turn1", "status": "completed", "durationMs": 900}})
        self.assertEqual(s.state, "idle")
        self.assertEqual(s.items[-1]["kind"], "turn")
        self.assertFalse(s.get("m")["streaming"])


class SmallHelpersTests(unittest.TestCase):
    def test_shell_wrapper_is_stripped(self):
        self.assertEqual(steerd.strip_shell_wrapper("/bin/zsh -lc \"git diff -- calc.py\""), "git diff -- calc.py")
        self.assertEqual(steerd.strip_shell_wrapper("/bin/bash -lc 'echo \"hi\"'"), 'echo "hi"')
        self.assertEqual(steerd.strip_shell_wrapper("ls"), "ls")

    def test_fuzzy_prefers_file_names(self):
        files = ["src/app/calc_view.swift", "docs/calculus.md", "calc.py", "tests/test_misc.py"]
        scored = sorted(((steerd.fuzzy_score("calc", f) or -1e9), f) for f in files)
        self.assertEqual(scored[-1][1], "calc.py")
        self.assertIsNone(steerd.fuzzy_score("zzz", "calc.py"))

    def test_stable_path_drops_per_shell_entries(self):
        self.assertEqual(steerd.stable_path("/a:/x/fnm_multishells/1/bin:/b:/a"), "/a:/b")


class SuggestionAndCommandTests(unittest.TestCase):
    def test_prompt_suggestion_shows_only_while_idle(self):
        hub = FakeHub()
        s = steerd.ClaudeSession(hub, {"sid": "s1", "cwd": "/tmp"})
        s.on_message({"type": "prompt_suggestion", "suggestion": "run the tests"})
        self.assertEqual(s.summary()["suggestion"], "run the tests")
        s.set_state("running")
        self.assertIsNone(s.summary()["suggestion"])
        s.set_state("idle")
        self.assertIsNone(s.summary()["suggestion"])

    def test_suggestion_ignored_while_running(self):
        s = steerd.ClaudeSession(FakeHub(), {"sid": "s2", "cwd": "/tmp"})
        s.set_state("running")
        s.on_message({"type": "prompt_suggestion", "suggestion": "late"})
        s.set_state("idle")
        self.assertIsNone(s.summary()["suggestion"])

    def test_claude_commands_put_skills_first(self):
        info = {"commands": [
            {"name": "compact", "description": "Free up context", "argumentHint": "<x>", "builtin": True},
            {"name": "deep-research", "description": "Research (user)", "argumentHint": ""},
        ]}
        out = steerd.claude_command_list(info)
        self.assertEqual([c["name"] for c in out], ["deep-research", "compact"])
        self.assertEqual([c["kind"] for c in out], ["skill", "builtin"])

    def test_open_terminal_sessions_need_a_live_interactive_pid(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as root:
            entries = {
                "1.json": {"pid": os.getpid(), "sessionId": "live", "kind": "interactive", "entrypoint": "cli"},
                "2.json": {"pid": os.getpid(), "sessionId": "sdk", "kind": "sdk"},
                "5.json": {"pid": os.getpid(), "sessionId": "print", "kind": "interactive", "entrypoint": "sdk-cli"},
                "3.json": {"pid": 2 ** 22 + 7, "sessionId": "gone", "kind": "interactive"},
            }
            for name, d in entries.items():
                with open(os.path.join(root, name), "w") as f:
                    json.dump(d, f)
            with open(os.path.join(root, "4.json"), "w") as f:
                f.write("{broken")
            self.assertEqual(steerd.claude_open_sessions(root), {"live": os.getpid()})

    def test_skill_mentions(self):
        found = steerd.SKILL_MENTION.findall("use $pdf and $skill-creator. not a$b or $$x, cost $5")
        self.assertEqual([n.rstrip(".:-") for n in found], ["pdf", "skill-creator", "5"])


if __name__ == "__main__":
    unittest.main()
