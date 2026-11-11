import SwiftUI

struct ChatRowView: View {
    let row: ChatRow
    let isLast: Bool

    var body: some View {
        switch row {
        case .user(let item): UserBubble(item: item)
        case .assistant(let item): AssistantMessage(item: item)
        case .tools(_, let tools): ToolGroupRow(tools: tools, startExpanded: false)
        case .plan(let item): PlanRow(item: item)
        case .notice(let item): NoticeRow(item: item)
        case .turn(let item): TurnRow(item: item)
        }
    }
}

struct UserBubble: View {
    let item: TimelineItem

    var body: some View {
        HStack {
            Spacer(minLength: 48)
            VStack(alignment: .trailing, spacing: 4) {
                if let images = item.images, images > 0 {
                    Label(String(localized: "\(images) 张图片"), systemImage: "photo")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let text = item.text, !text.isEmpty {
                    Text(text)
                        .textSelection(.enabled)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(RoundedRectangle(cornerRadius: 18).fill(Color(.secondarySystemBackground)))
                }
                if item.queued == true {
                    Label("排队中，这一步做完就处理", systemImage: "clock").font(.caption2).foregroundStyle(.secondary)
                }
                if item.failed == true {
                    Label("没发出去", systemImage: "exclamationmark.circle").font(.caption2).foregroundStyle(.red)
                }
            }
        }
    }
}

struct AssistantMessage: View {
    let item: TimelineItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            MarkdownText(text: item.text ?? "")
            if item.streaming == true {
                ProgressView().controlSize(.mini)
            }
        }
    }
}

/// Consecutive tool calls folded into one tappable summary line.
struct ToolGroupRow: View {
    let tools: [TimelineItem]
    @State var expanded: Bool
    @State private var detail: TimelineItem?

    init(tools: [TimelineItem], startExpanded: Bool) {
        self.tools = tools
        _expanded = State(initialValue: startExpanded)
    }

    private var running: TimelineItem? { tools.last { $0.isRunning } }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.snappy) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    if let running {
                        ProgressView().controlSize(.small)
                        Text(ToolText.activity(running.category, running.target))
                            .lineLimit(1)
                    } else {
                        Image(systemName: tools.contains { $0.status == "error" } ? "exclamationmark.circle" : "checkmark.circle")
                            .foregroundStyle(tools.contains { $0.status == "error" } ? Color.orange : Color.secondary)
                        Text(ToolText.summary(tools)).lineLimit(2)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                        .foregroundStyle(.tertiary)
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.vertical, 8)
                .padding(.horizontal, 12)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground).opacity(0.7)))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(tools) { tool in
                        Button {
                            detail = tool
                        } label: {
                            ToolLine(tool: tool)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.leading, 6)
                .padding(.bottom, 6)
            }
        }
        .sheet(item: $detail) { tool in
            ToolDetailSheet(tool: tool)
        }
    }
}

struct ToolLine: View {
    let tool: TimelineItem

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            statusIcon.frame(width: 16)
            Image(systemName: ToolText.icon(tool.category)).font(.caption).foregroundStyle(.secondary).frame(width: 16)
            Text(tool.target?.isEmpty == false ? tool.target ?? "" : ToolText.verb(tool.category))
                .font(.system(.footnote, design: .monospaced))
                .lineLimit(2)
                .foregroundStyle(.primary)
            Spacer(minLength: 0)
            if tool.add != nil || tool.del != nil {
                DiffCounts(add: tool.add, del: tool.del)
            }
        }
        .padding(.vertical, 5)
        .padding(.horizontal, 6)
        .contentShape(Rectangle())
    }

    @ViewBuilder private var statusIcon: some View {
        switch tool.status {
        case "running": ProgressView().controlSize(.mini)
        case "error": Image(systemName: "xmark.circle.fill").foregroundStyle(.red).font(.caption)
        case "declined": Image(systemName: "nosign").foregroundStyle(.orange).font(.caption)
        default: Image(systemName: "checkmark").foregroundStyle(.green).font(.caption)
        }
    }
}

struct ToolDetailSheet: View {
    let tool: TimelineItem
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let target = tool.command ?? tool.target, !target.isEmpty {
                        Text(target)
                            .font(.system(.callout, design: .monospaced))
                            .textSelection(.enabled)
                            .padding(12)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 10).fill(Color(.secondarySystemBackground)))
                    }
                    if let detail = tool.detail, !detail.isEmpty {
                        Text(detail).font(.footnote).foregroundStyle(.secondary)
                    }
                    if let diff = tool.diff, !diff.isEmpty {
                        HStack {
                            Text("改动").font(.headline)
                            DiffCounts(add: tool.add, del: tool.del)
                        }
                        DiffView(diff: diff)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    if let output = tool.output, !output.isEmpty, tool.category != "edit" || tool.status == "error" {
                        HStack {
                            Text("输出").font(.headline)
                            if let code = tool.exitCode, code != 0 {
                                Text("退出码 \(code)").font(.caption).foregroundStyle(.red)
                            }
                        }
                        Text(output)
                            .font(.system(size: 12, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(10)
                            .background(RoundedRectangle(cornerRadius: 10).fill(Color(.secondarySystemBackground)))
                    }
                }
                .padding(16)
            }
            .navigationTitle(ToolText.verb(tool.category))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

struct PlanRow: View {
    let item: TimelineItem

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("计划", systemImage: "list.bullet.clipboard").font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
            if let steps = item.steps, !steps.isEmpty {
                ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: icon(step.status))
                            .foregroundStyle(step.status == "completed" ? Color.green : step.status == "inProgress" ? Color.accentColor : Color.secondary)
                            .font(.footnote)
                        Text(step.step)
                            .font(.callout)
                            .strikethrough(step.status == "completed", color: .secondary)
                            .foregroundStyle(step.status == "completed" ? .secondary : .primary)
                    }
                }
            }
            if let text = item.text, !text.isEmpty {
                MarkdownText(text: text)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14).strokeBorder(Color(.separator)))
    }

    private func icon(_ status: String) -> String {
        switch status {
        case "completed": "checkmark.circle.fill"
        case "inProgress": "circle.dotted.circle"
        default: "circle"
        }
    }
}

struct NoticeRow: View {
    let item: TimelineItem

    var body: some View {
        VStack(spacing: 2) {
            Text(title).font(.caption.weight(.medium))
            if let text = item.text, !text.isEmpty {
                Text(text).font(.caption2).multilineTextAlignment(.center).lineLimit(6)
            }
        }
        .foregroundStyle(item.code == "compacted" || item.code == "interrupted" ? Color.secondary : Color.orange)
        .frame(maxWidth: .infinity)
    }

    private var title: String {
        switch item.code {
        case "compacted": String(localized: "上下文已压缩")
        case "interrupted": String(localized: "已中断")
        case "agent_exited": String(localized: "agent 进程退出了")
        case "send_failed": String(localized: "消息没发出去")
        case "elicitation_declined": String(localized: "已拒绝一个 MCP 表单请求")
        default: String(localized: "出了点问题")
        }
    }
}

struct TurnRow: View {
    let item: TimelineItem

    var body: some View {
        HStack(spacing: 6) {
            switch item.status {
            case "interrupted":
                Image(systemName: "stop.circle")
                Text("已停止")
            case "error":
                Image(systemName: "exclamationmark.triangle")
                Text(item.text?.isEmpty == false ? item.text ?? "" : String(localized: "这一轮出错了")).lineLimit(3)
            default:
                Image(systemName: "checkmark")
                if let ms = item.durationMs, ms > 0 {
                    Text("完成 · \(Durations.text(ms: ms))")
                } else {
                    Text("完成")
                }
            }
        }
        .font(.caption2)
        .foregroundStyle(item.status == "error" ? Color.red : Color.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        if item.status == "error", item.text?.contains("Not logged in") == true {
            Text("主机上的助手读不到 Claude 的登录状态。macOS 上 Claude 的登录信息在钥匙串里，只有登录了桌面的会话能读到：在电脑上登录桌面后，回到 App 下拉刷新重连。")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }
}

struct ThinkingRow: View {
    let since: Double?

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("在做了")
                if let since {
                    Text(Durations.clock(since: since, now: context.date)).monospacedDigit()
                }
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
        }
    }
}

/// Every file change in the session, newest last, each with its diff.
struct ChangesSheet: View {
    let edits: [TimelineItem]
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if edits.isEmpty {
                    ContentUnavailableView("还没有改动", systemImage: "doc.text.magnifyingglass")
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            ForEach(edits) { edit in
                                VStack(alignment: .leading, spacing: 6) {
                                    HStack {
                                        Text(edit.target ?? "").font(.subheadline.monospaced().weight(.semibold)).lineLimit(2)
                                        Spacer()
                                        DiffCounts(add: edit.add, del: edit.del)
                                    }
                                    DiffView(diff: edit.diff ?? "").clipShape(RoundedRectangle(cornerRadius: 8))
                                }
                            }
                        }
                        .padding(16)
                    }
                }
            }
            .navigationTitle(totalTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
    }

    private var totalTitle: String {
        let files = Set(edits.compactMap(\.target)).count
        let add = edits.compactMap(\.add).reduce(0, +)
        let del = edits.compactMap(\.del).reduce(0, +)
        return String(localized: "\(files) 个文件 +\(add) −\(del)")
    }
}
