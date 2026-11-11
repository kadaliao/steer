import Foundation

enum Agents {
    static func name(_ agent: String) -> String {
        agent == "codex" ? "Codex" : "Claude"
    }

    static let all = ["claude", "codex"]

    static func defaultMode(_ agent: String) -> String {
        agent == "codex" ? "auto" : "default"
    }

    static func modeLabel(_ mode: String?, agent: String) -> String {
        let mode = mode ?? defaultMode(agent)
        if agent == "codex" {
            switch mode {
            case "untrusted": return String(localized: "都问我")
            case "readOnly": return String(localized: "只读")
            case "full": return String(localized: "完全访问")
            default: return String(localized: "自动")
            }
        }
        switch mode {
        case "acceptEdits": return String(localized: "自动改文件")
        case "auto": return String(localized: "自动")
        case "plan": return String(localized: "计划")
        case "bypassPermissions": return String(localized: "全自动")
        case "dontAsk": return String(localized: "不询问")
        default: return String(localized: "询问")
        }
    }

    static func modeHint(_ mode: String, agent: String) -> String {
        if agent == "codex" {
            switch mode {
            case "untrusted": return String(localized: "除了只读命令，都先问你")
            case "readOnly": return String(localized: "只能读，不能改文件")
            case "full": return String(localized: "不问也不限制，小心使用")
            default: return String(localized: "在项目里随便改，越界才问")
            }
        }
        switch mode {
        case "acceptEdits": return String(localized: "改文件不问，跑命令还会问")
        case "auto": return String(localized: "由安全检查决定，少打扰")
        case "plan": return String(localized: "先出计划，你批准后再动手")
        case "bypassPermissions": return String(localized: "什么都不问，小心使用")
        default: return String(localized: "改文件和跑命令前都问你")
        }
    }

    static func modelLabel(_ model: String?, info: AgentInfo?) -> String {
        guard let model, !model.isEmpty else { return String(localized: "默认模型") }
        if let option = info?.models.first(where: { $0.id == model }), let name = option.name {
            return name.replacingOccurrences(of: " (recommended)", with: "")
        }
        return model
    }
}

enum Durations {
    static func text(ms: Double) -> String {
        let seconds = Int((ms / 1000).rounded())
        if seconds < 60 { return String(localized: "\(max(seconds, 1)) 秒") }
        let minutes = seconds / 60
        if minutes < 60 { return String(localized: "\(minutes) 分 \(seconds % 60) 秒") }
        return String(localized: "\(minutes / 60) 小时 \(minutes % 60) 分")
    }

    static func clock(since ms: Double, now: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince1970 - ms / 1000))
        if seconds >= 3600 { return String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60) }
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
}

enum ToolText {
    static func icon(_ category: String?) -> String {
        switch category {
        case "command": "terminal"
        case "read": "doc.text"
        case "edit": "pencil"
        case "search": "magnifyingglass"
        case "web", "network": "globe"
        case "agent": "person.2"
        case "skill": "sparkles"
        case "mcp": "puzzlepiece.extension"
        case "permissions": "lock.open"
        default: "wrench.and.screwdriver"
        }
    }

    static func verb(_ category: String?) -> String {
        switch category {
        case "command": String(localized: "运行")
        case "read": String(localized: "读取")
        case "edit": String(localized: "修改")
        case "search": String(localized: "搜索")
        case "web": String(localized: "联网")
        case "agent": String(localized: "子任务")
        case "skill": String(localized: "技能")
        case "mcp": "MCP"
        default: String(localized: "工具")
        }
    }

    static func requestTitle(_ request: AgentRequest) -> String {
        switch request.category {
        case "command": String(localized: "要运行命令")
        case "edit": String(localized: "要修改文件")
        case "read": String(localized: "要读取")
        case "search": String(localized: "要搜索")
        case "web": String(localized: "要访问网页")
        case "network": String(localized: "要联网")
        case "permissions": String(localized: "要额外权限")
        case "mcp": String(localized: "要调用 MCP 工具")
        case "agent": String(localized: "要启动子任务")
        default: String(localized: "要使用 \(request.tool ?? "工具")")
        }
    }

    /// "1 分 20 秒 · 读 3 个文件 · 改 2 个 +24 −6"
    static func summary(_ tools: [TimelineItem]) -> String {
        var parts: [String] = []
        if let first = tools.compactMap(\.ts).min(), let last = tools.compactMap({ $0.endTs ?? $0.ts }).max(), last - first >= 1000 {
            parts.append(Durations.text(ms: last - first))
        }
        let edits = tools.filter { $0.category == "edit" }
        if !edits.isEmpty {
            let files = Set(edits.compactMap(\.target)).count
            let add = edits.compactMap(\.add).reduce(0, +)
            let del = edits.compactMap(\.del).reduce(0, +)
            var text = String(localized: "改 \(files) 个文件")
            if add + del > 0 { text += " +\(add) −\(del)" }
            parts.append(text)
        }
        let counts: [(String, String)] = [
            ("read", String(localized: "读 %d 个文件")),
            ("command", String(localized: "运行 %d 条命令")),
            ("search", String(localized: "搜索 %d 次")),
            ("web", String(localized: "联网 %d 次")),
            ("agent", String(localized: "子任务 %d 个")),
        ]
        for (category, format) in counts {
            let n = tools.filter { $0.category == category }.count
            if n > 0 { parts.append(String(format: format, n)) }
        }
        let known: Set<String> = ["edit", "read", "command", "search", "web", "agent"]
        let other = tools.filter { !known.contains($0.category ?? "") }.count
        if other > 0 { parts.append(String(format: String(localized: "其他 %d 步"), other)) }
        return parts.joined(separator: " · ")
    }

    static func activity(_ category: String?, _ target: String?) -> String {
        let target = target ?? ""
        return String(localized: "正在\(verb(category)) \(target)")
    }
}

/// What the chat list renders: consecutive tool calls fold into one row.
enum ChatRow: Identifiable {
    case user(TimelineItem)
    case assistant(TimelineItem)
    case tools(String, [TimelineItem])
    case plan(TimelineItem)
    case notice(TimelineItem)
    case turn(TimelineItem)

    var id: String {
        switch self {
        case .user(let i), .assistant(let i), .plan(let i), .notice(let i), .turn(let i): i.id
        case .tools(let id, _): "tools-" + id
        }
    }

    static func rows(from items: [TimelineItem]) -> [ChatRow] {
        var rows: [ChatRow] = []
        var group: [TimelineItem] = []
        func flush() {
            if let first = group.first {
                rows.append(.tools(first.id, group))
                group = []
            }
        }
        for item in items {
            switch item.kind {
            case "tool":
                if item.category == "meta" || item.category == "todo" { continue }
                group.append(item)
            case "assistant":
                if (item.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, item.streaming != true {
                    continue
                }
                flush()
                rows.append(.assistant(item))
            case "user":
                flush()
                rows.append(.user(item))
            case "plan":
                flush()
                rows.append(.plan(item))
            case "notice":
                flush()
                rows.append(.notice(item))
            case "turn":
                flush()
                rows.append(.turn(item))
            default:
                continue
            }
        }
        flush()
        return rows
    }
}
