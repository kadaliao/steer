import SwiftUI

/// "+" lands here: the keyboard is up, project and agent default to the last
/// ones used, and sending the first message creates the session.
struct NewChatView: View {
    @Environment(AppModel.self) private var app
    let onCreated: (String) -> Void

    @State private var agent = Prefs.lastAgent
    @State private var cwd = Prefs.lastProject
    @State private var model: String? = Prefs.lastModel(Prefs.lastAgent)
    @State private var mode: String? = Prefs.lastMode(Prefs.lastAgent)
    @State private var info: AgentInfo?
    @State private var text = Prefs.draft("new")
    @State private var attachments: [ImageAttachment] = []
    @State private var creating = false
    @State private var error: String?
    @State private var pickingProject = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 14) {
            Spacer()
            AgentBadge(agent: agent, size: 44)
            Text(cwd == nil ? String(localized: "先选一个项目") : String(localized: "想让 \(Agents.name(agent)) 做什么？"))
                .font(.title3.weight(.semibold))
            if let cwd {
                Text(cwd.replacingOccurrences(of: app.hello?.home ?? "~", with: "~"))
                    .font(.footnote.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.head).padding(.horizontal, 24)
            }
            if let error {
                Text(error).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center).padding(.horizontal)
            }
            Spacer()
        }
        .frame(maxWidth: .infinity)
        .contentShape(Rectangle())
        .onTapGesture { focused = false }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 8) {
                HStack(spacing: 8) {
                    Button {
                        pickingProject = true
                    } label: {
                        ChipLabel(text: projectName, systemImage: "folder", prominent: cwd == nil)
                    }
                    .buttonStyle(.plain)
                    Menu {
                        ForEach(Agents.all, id: \.self) { option in
                            Button {
                                switchAgent(option)
                            } label: {
                                if option == agent {
                                    Label(Agents.name(option), systemImage: "checkmark")
                                } else {
                                    Text(Agents.name(option))
                                }
                            }
                            .disabled(!(app.hello?.has(option) ?? true))
                        }
                    } label: {
                        ChipLabel(text: Agents.name(agent), systemImage: agent == "codex" ? "circle.hexagongrid" : "asterisk")
                    }
                    Spacer()
                }
                .padding(.horizontal, 4)
                Composer(
                    text: $text, attachments: $attachments, focused: $focused, agent: agent, cwd: cwd,
                    placeholder: String(localized: "描述你要做的事…"), isRunning: false, showQuickPhrases: false,
                    canSend: app.isConnected && cwd != nil && !creating,
                    onSend: create, onStop: {}, onQuickPhrase: { _ in }
                ) {
                    ModeMenu(agent: agent, mode: mode, info: info) { mode = $0 }
                    ModelMenu(agent: agent, model: model, info: info) { model = $0 }
                }
                if creating {
                    ProgressView("正在启动 \(Agents.name(agent))…").font(.caption)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(.bar)
        }
        .navigationTitle("新会话")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $pickingProject) {
            ProjectPicker(selected: cwd) { path in
                cwd = path
                Prefs.lastProject = path
            }
        }
        .task {
            info = await app.info(for: agent)
            if cwd == nil {
                await app.loadProjects()
                cwd = app.projects.first?.path
            }
            focused = true
        }
        .onChange(of: text) { _, value in Prefs.setDraft(value, "new") }
    }

    private var projectName: String {
        guard let cwd else { return String(localized: "选择项目") }
        return (cwd as NSString).lastPathComponent
    }

    private func switchAgent(_ option: String) {
        agent = option
        model = Prefs.lastModel(option)
        mode = Prefs.lastMode(option)
        info = nil
        Task { info = await app.info(for: option) }
    }

    private func create() {
        guard let cwd else {
            pickingProject = true
            return
        }
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty || !attachments.isEmpty else { return }
        creating = true
        error = nil
        Task {
            do {
                let summary = try await app.createSession(
                    agent: agent, cwd: cwd, text: message, images: attachments, model: model, mode: mode)
                Prefs.setLastModel(model, agent)
                Prefs.setLastMode(mode, agent)
                text = ""
                Prefs.setDraft("", "new")
                creating = false
                onCreated(summary.sid)
            } catch {
                creating = false
                self.error = error.localizedDescription
            }
        }
    }
}

struct ProjectPicker: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let selected: String?
    let choose: (String) -> Void
    @State private var query = ""

    private var filtered: [ProjectEntry] {
        guard !query.isEmpty else { return app.projects }
        return app.projects.filter { $0.path.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        NavigationStack {
            List {
                if query.hasPrefix("/") || query.hasPrefix("~") {
                    Button {
                        pick(query)
                    } label: {
                        Label("使用 \(query)", systemImage: "arrow.right.circle")
                    }
                }
                Section("最近") {
                    ForEach(filtered) { project in
                        Button {
                            pick(project.path)
                        } label: {
                            HStack {
                                Image(systemName: project.git == true ? "folder.badge.gearshape" : "folder")
                                    .foregroundStyle(Color.accentColor)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(project.name).foregroundStyle(.primary)
                                    Text(shortPath(project.path)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer()
                                if project.path == selected {
                                    Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                                }
                            }
                        }
                    }
                }
                Section {
                    NavigationLink {
                        FolderBrowser(path: app.hello?.home ?? "~") { path in pick(path) }
                    } label: {
                        Label("浏览文件夹…", systemImage: "folder.badge.plus")
                    }
                }
            }
            .searchable(text: $query, prompt: "搜索，或输入路径")
            .navigationTitle("选择项目")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
            }
            .task { await app.loadProjects() }
        }
    }

    private func shortPath(_ path: String) -> String {
        guard let home = app.hello?.home else { return path }
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    private func pick(_ path: String) {
        var path = path
        if path.hasPrefix("~"), let home = app.hello?.home {
            path = home + path.dropFirst()
        }
        choose(path)
        dismiss()
    }
}

struct FolderBrowser: View {
    @Environment(AppModel.self) private var app
    let path: String
    let choose: (String) -> Void
    @State private var listing: DirListing?
    @State private var error: String?

    var body: some View {
        List {
            if let listing {
                Section {
                    Button {
                        choose(listing.path)
                    } label: {
                        Label("就用这个文件夹", systemImage: "checkmark.circle.fill")
                    }
                }
                Section {
                    ForEach(listing.entries) { entry in
                        NavigationLink {
                            FolderBrowser(path: entry.path, choose: choose)
                        } label: {
                            Label(entry.name, systemImage: entry.git == true ? "folder.badge.gearshape" : "folder")
                        }
                    }
                }
            } else if let error {
                Text(error).foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .navigationTitle((path as NSString).lastPathComponent)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            do {
                listing = try await app.call("dirs.list", ["path": .string(path)])
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
