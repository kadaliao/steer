import SwiftUI

/// Resolves a session id to its chat once the list knows about it.
struct ChatScreen: View {
    @Environment(AppModel.self) private var app
    let sid: String

    var body: some View {
        if let chat = app.chat(sid: sid) {
            ChatView(chat: chat)
        } else {
            ProgressView().task { await app.refreshSessions() }
        }
    }
}

struct ChatView: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    @State private var text = ""
    @State private var attachments: [ImageAttachment] = []
    @State private var info: AgentInfo?
    @State private var renaming = false
    @State private var newTitle = ""
    @State private var showChanges = false
    @FocusState private var focused: Bool

    private var rows: [ChatRow] { ChatRow.rows(from: chat.items) }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                if chat.earlier > 0 {
                    Text("更早的 \(chat.earlier) 条没有显示").font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity)
                }
                if !chat.loaded {
                    if let error = chat.loadError {
                        Text(error).font(.footnote).foregroundStyle(.secondary).frame(maxWidth: .infinity)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                    }
                }
                let rows = rows
                ForEach(rows) { row in
                    ChatRowView(row: row, isLast: row.id == rows.last?.id)
                }
                if chat.isRunning, !lastRowIsLive(rows) {
                    ThinkingRow(since: chat.summary.runningSince)
                }
                if let error = chat.summary.error, !chat.isRunning {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
        .defaultScrollAnchor(.bottom)
        .defaultScrollAnchor(.bottom, for: .sizeChanges)
        .scrollDismissesKeyboard(.interactively)
        .safeAreaInset(edge: .bottom, spacing: 0) { dock }
        .toolbar {
            ToolbarItem(placement: .principal) { titleView }
            ToolbarItem(placement: .topBarTrailing) { menu }
        }
        .navigationBarTitleDisplayMode(.inline)
        .task {
            chat.isVisible = true
            text = Prefs.draft(chat.sid)
            await chat.load()
            info = await app.info(for: chat.agent)
        }
        .onChange(of: text) { _, value in Prefs.setDraft(value, chat.sid) }
        .onDisappear {
            chat.close()
            app.markSeen(chat.summary)
        }
        .sheet(isPresented: $showChanges) {
            ChangesSheet(edits: chat.items.filter { $0.kind == "tool" && $0.category == "edit" && $0.diff?.isEmpty == false })
        }
        .alert("重命名", isPresented: $renaming) {
            TextField("标题", text: $newTitle)
            Button("取消", role: .cancel) {}
            Button("保存") { Task { await chat.rename(newTitle) } }
        }
    }

    private func lastRowIsLive(_ rows: [ChatRow]) -> Bool {
        guard let last = rows.last else { return false }
        switch last {
        case .assistant(let item): return item.streaming == true
        case .tools(_, let tools): return tools.contains { $0.isRunning }
        default: return false
        }
    }

    private var titleView: some View {
        VStack(spacing: 0) {
            Text(chat.summary.title.isEmpty ? String(localized: "新会话") : chat.summary.title)
                .font(.subheadline.weight(.semibold)).lineLimit(1)
            Text("\(chat.summary.projectName) · \(Agents.name(chat.agent))")
                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: 240)
    }

    private var menu: some View {
        Menu {
            Button {
                newTitle = chat.summary.title
                renaming = true
            } label: { Label("重命名", systemImage: "pencil") }
            if chat.isRunning {
                Button(role: .destructive) {
                    Task { await chat.interrupt() }
                } label: { Label("停止", systemImage: "stop.circle") }
            }
            Button {
                showChanges = true
            } label: { Label("本会话的改动", systemImage: "plusminus") }
            Button {
                UIPasteboard.general.string = resumeCommand
            } label: { Label("复制“在电脑上继续”的命令", systemImage: "desktopcomputer") }
            Button {
                Task { await chat.load() }
            } label: { Label("刷新", systemImage: "arrow.clockwise") }
        } label: {
            Image(systemName: "ellipsis.circle")
        }
    }

    private var resumeCommand: String {
        let cd = "cd " + shellQuote(chat.summary.cwd)
        if chat.agent == "codex" { return cd + " && codex resume " + chat.sid }
        return cd + " && claude --resume " + chat.sid
    }

    private func shellQuote(_ path: String) -> String {
        "'" + path.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    // MARK: dock

    private var dock: some View {
        VStack(spacing: 8) {
            if let error = chat.actionError {
                Text(error).font(.caption).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading)
            }
            if let request = chat.requests.first {
                RequestCard(request: request, chat: chat) { focused = true }
                    .id(request.rid)
                if chat.requests.count > 1 {
                    Text("还有 \(chat.requests.count - 1) 个请求").font(.caption2).foregroundStyle(.secondary)
                }
            }
            intentBanner
            if desktopMayBeOpen {
                Label("这个会话几分钟前还在电脑上动过。如果电脑上还开着，两边同时发消息会让记录分叉。", systemImage: "desktopcomputer")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 6)
            }
            Composer(
                text: $text, attachments: $attachments, focused: $focused, agent: chat.agent, cwd: chat.summary.cwd,
                placeholder: placeholder, isRunning: chat.isRunning,
                showQuickPhrases: chat.requests.isEmpty && chat.intent == .message, canSend: app.isConnected,
                onSend: send, onStop: { Task { await chat.interrupt() } },
                onQuickPhrase: { phrase in
                    Task { _ = await chat.send(phrase, images: []) }
                }
            ) {
                ModeMenu(agent: chat.agent, mode: chat.summary.mode, info: info) { mode in
                    Task { await chat.setMode(mode) }
                }
                ModelMenu(agent: chat.agent, model: chat.summary.model, info: info) { model in
                    Task { await chat.setModel(model) }
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.top, 6)
        .padding(.bottom, 6)
        .background(.bar)
    }

    private var desktopMayBeOpen: Bool {
        guard chat.summary.isDesktop, chat.summary.live != true, let updated = chat.summary.updatedDate else { return false }
        return Date().timeIntervalSince(updated) < 10 * 60
    }

    @ViewBuilder private var intentBanner: some View {
        switch chat.intent {
        case .message:
            EmptyView()
        case .denyWithNote:
            intentLabel(String(localized: "写下你想让它怎么做，发送即拒绝这一步"))
        case .answer:
            intentLabel(String(localized: "直接输入你的回答"))
        }
    }

    private func intentLabel(_ text: String) -> some View {
        HStack {
            Image(systemName: "arrowshape.turn.up.left").foregroundStyle(Color.needsYou)
            Text(text).font(.caption)
            Spacer()
            Button("取消") { chat.intent = .message }.font(.caption)
        }
        .padding(.horizontal, 6)
    }

    private var placeholder: String {
        switch chat.intent {
        case .denyWithNote: String(localized: "换个做法…")
        case .answer: String(localized: "你的回答…")
        case .message:
            chat.isRunning ? String(localized: "补充说明，它会接着看到…") : String(localized: "给 \(Agents.name(chat.agent)) 发消息…")
        }
    }

    private func send() {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let images = attachments
        guard !message.isEmpty || !images.isEmpty else { return }
        text = ""
        attachments = []
        Task {
            let ok = await chat.send(message, images: images)
            if !ok, text.isEmpty {
                text = message
                attachments = images
            }
        }
    }
}

struct ModeMenu: View {
    let agent: String
    let mode: String?
    let info: AgentInfo?
    let choose: (String) -> Void

    private var modes: [String] {
        info?.modes ?? (agent == "codex" ? ["untrusted", "auto", "readOnly", "full"] : ["default", "acceptEdits", "auto", "plan", "bypassPermissions"])
    }

    var body: some View {
        Menu {
            ForEach(modes, id: \.self) { option in
                Button {
                    choose(option)
                } label: {
                    if option == (mode ?? Agents.defaultMode(agent)) {
                        Label(Agents.modeLabel(option, agent: agent), systemImage: "checkmark")
                    } else {
                        Text(Agents.modeLabel(option, agent: agent))
                    }
                    Text(Agents.modeHint(option, agent: agent))
                }
            }
        } label: {
            ChipLabel(text: Agents.modeLabel(mode, agent: agent), systemImage: "hand.raised")
        }
    }
}

struct ModelMenu: View {
    let agent: String
    let model: String?
    let info: AgentInfo?
    let choose: (String) -> Void

    var body: some View {
        Menu {
            ForEach(info?.models ?? []) { option in
                Button {
                    choose(option.id)
                } label: {
                    if option.id == model {
                        Label(option.name ?? option.id, systemImage: "checkmark")
                    } else {
                        Text(option.name ?? option.id)
                    }
                    if let description = option.description, !description.isEmpty {
                        Text(description)
                    }
                }
            }
        } label: {
            ChipLabel(text: Agents.modelLabel(model, info: info), systemImage: "cpu")
        }
    }
}
