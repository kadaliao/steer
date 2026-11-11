import Foundation
import Testing

@testable import Steer

struct MarkdownTests {
    @Test func splitsCodeFencesListsAndParagraphs() {
        let blocks = MarkdownBlock.parse("""
            # Title
            Some **bold** text
            continues here.

            - one
            - [x] done
            1. first

            ```swift
            let x = 1
            ```
            """)
        #expect(blocks == [
            .heading(1, "Title"),
            .paragraph("Some **bold** text\ncontinues here."),
            .listItem("•", 0, "one"),
            .listItem("☑", 0, "done"),
            .listItem("1.", 0, "first"),
            .code("swift", "let x = 1"),
        ])
    }

    @Test func keepsAnUnclosedFenceWhileStreaming() {
        let blocks = MarkdownBlock.parse("Look:\n```\npartial")
        #expect(blocks == [.paragraph("Look:"), .code("", "partial")])
    }
}

struct TimelineTests {
    private func tool(_ id: String, _ category: String, add: Int? = nil, del: Int? = nil, target: String = "a.swift",
                      status: String = "ok") -> TimelineItem {
        TimelineItem(id: id, kind: "tool", category: category, target: target, status: status, add: add, del: del)
    }

    @Test func foldsConsecutiveToolsAndHidesMetaTools() {
        let items = [
            TimelineItem(id: "u1", kind: "user", text: "fix it"),
            tool("t1", "read"),
            tool("t2", "meta"),
            tool("t3", "edit", add: 3, del: 1),
            TimelineItem(id: "a1", kind: "assistant", text: "Done."),
            TimelineItem(id: "turn", kind: "turn", status: "completed"),
        ]
        let rows = ChatRow.rows(from: items)
        #expect(rows.map(\.id) == ["u1", "tools-t1", "a1", "turn"])
        if case .tools(_, let tools) = rows[1] {
            #expect(tools.map(\.id) == ["t1", "t3"])
        } else {
            Issue.record("expected a tool group")
        }
    }

    @Test func summarisesEditsReadsAndCommands() {
        let text = ToolText.summary([
            tool("1", "read"), tool("2", "read", target: "b.swift"),
            tool("3", "edit", add: 24, del: 6), tool("4", "edit", add: 1, del: 0, target: "b.swift"),
            tool("5", "command", target: "npm test"),
        ])
        #expect(text == "改 2 个文件 +25 −6 · 读 2 个文件 · 运行 1 条命令")
    }

    @Test func dropsEmptyFinishedAssistantText() {
        let rows = ChatRow.rows(from: [
            TimelineItem(id: "a0", kind: "assistant", text: "", streaming: false),
            TimelineItem(id: "a1", kind: "assistant", text: "", streaming: true),
        ])
        #expect(rows.map(\.id) == ["a1"])
    }
}

struct WireTests {
    @Test func decodesASnapshotAndEvents() throws {
        let json = """
            {"session": {"sid": "s1", "agent": "claude", "cwd": "/w/app", "title": "Fix", "state": "waiting",
             "needs": "permission", "lastText": "", "activity": null, "updatedAt": 1794393250328, "runningSince": null,
             "model": null, "mode": "default", "source": "steer", "live": true, "error": null, "future": 1},
             "items": [{"id": "t1", "kind": "tool", "tool": "Edit", "category": "edit", "target": "calc.py",
             "status": "running", "diff": "+x", "add": 1, "del": 0, "ts": 1794393250328}],
             "earlier": 0,
             "requests": [{"rid": "r1", "kind": "question", "questions": [{"id": "q", "question": "Tabs?",
             "options": [{"label": "Tabs", "description": ""}], "multiSelect": false, "allowOther": true}]}]}
            """
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        #expect(snapshot.session.isWaiting)
        #expect(snapshot.session.projectName == "app")
        #expect(snapshot.items.first?.add == 1)
        #expect(snapshot.requests.first?.questions?.first?.options.first?.label == "Tabs")

        let delta = try JSONDecoder().decode(EventFrame.self, from: Data(#"{"ev":"delta","sid":"s1","itemId":"m:t0","text":"hi"}"#.utf8))
        #expect(delta.itemId == "m:t0")
    }

    @Test func encodesIntegerParamsWithoutAFraction() throws {
        let data = try JSONEncoder().encode(["id": JSONValue.number(3), "limit": 30])
        let text = String(decoding: data, as: UTF8.self)
        #expect(text.contains("\"id\":3"))
        #expect(!text.contains("3.0"))
    }

    @MainActor @Test func chatAppliesDeltasAndResolvesRequests() {
        let summary = SessionSummary(sid: "s", agent: "codex", cwd: "/w", title: "", state: "running")
        let chat = ChatModel(summary: summary, app: AppModel())
        chat.apply(EventFrame(ev: "item", sid: "s", item: TimelineItem(id: "m", kind: "assistant", text: "He", streaming: true)))
        chat.apply(EventFrame(ev: "delta", sid: "s", itemId: "m", text: "llo"))
        #expect(chat.items.first?.text == "Hello")
        chat.apply(EventFrame(ev: "request", sid: "s", request: AgentRequest(rid: "x1", kind: "permission")))
        #expect(chat.requests.count == 1)
        chat.apply(EventFrame(ev: "resolved", sid: "s", rid: "x1"))
        #expect(chat.requests.isEmpty)
    }
}
