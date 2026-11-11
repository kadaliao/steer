import SwiftUI

/// "+" lands here: the keyboard is up, project and agent default to the last
/// ones used, and sending the first message creates the session.
struct NewChatView: View {
    @Environment(AppModel.self) private var app
    let onCreated: (UUID, String) -> Void

    @State private var hostID: UUID?
    @State private var agent = Prefs.lastAgent
    @State private var cwd: String?
    @State private var model: String? = Prefs.lastModel(Prefs.lastAgent)
    @State private var mode: String? = Prefs.lastMode(Prefs.lastAgent)
    @State private var info: AgentInfo?
    @State private var text = Prefs.draft("new")
    @State private var attachments: [ImageAttachment] = []
    @State private var creating = false
    @State private var error: String?
    @State private var pickingProject = false
    @FocusState private var focused: Bool

    private var conn: HostConnection? { app.connection(hostID) }

    var body: some View {
        VStack(spacing: 14) {
            Spacer()
            AgentBadge(agent: agent, size: 44)
            Text(cwd == nil ? String(localized: "先选一个项目") : String(localized: "想让 \(Agents.name(agent)) 做什么？"))
                .font(.title3.weight(.semibold))
            if let cwd {
                Text(conn?.shortPath(cwd) ?? cwd)
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
                    if app.hasManyHosts {
                        Menu {
                            ForEach(app.ordered) { option in
                                Button {
                                    switchHost(option.id)
                                } label: {
                                    if option.id == hostID {
                                        Label(option.name, systemImage: "checkmark")
                                    } else {
                                        Text(option.name)
                                    }
                                    Text(HostStatusText.text(option.status))
                                }
                            }
                        } label: {
                            ChipLabel(text: conn?.name ?? String(localized: "主机"), systemImage: "desktopcomputer")
                        }
                    }
                    Button {
                        pickingProject = true
                    } label: {
                        ChipLabel(text: projectName, systemImage: "folder", prominent: cwd == nil)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("projectChip")
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
                            .disabled(!(conn?.hello?.has(option) ?? true))
                        }
                    } label: {
                        ChipLabel(text: Agents.name(agent), systemImage: agent == "codex" ? "circle.hexagongrid" : "asterisk")
                    }
                    Spacer()
                }
                .padding(.horizontal, 4)
                Composer(
                    conn: conn, text: $text, attachments: $attachments, focused: $focused, agent: agent, cwd: cwd,
                    placeholder: String(localized: "描述你要做的事…"), isRunning: false,
                    canSend: (conn?.isConnected ?? false) && cwd != nil && !creating,
                    onSend: create, onStop: {}
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
            if let conn {
                ProjectPicker(conn: conn, selected: cwd) { path in
                    cwd = path
                    Prefs.setLastProject(path, host: conn.id)
                }
            }
        }
        .task {
            if hostID == nil { hostID = app.hostForNewChat?.id }
            await prepareHost()
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
        Task { info = await conn?.info(for: option) }
    }

    private func switchHost(_ id: UUID) {
        guard id != hostID else { return }
        hostID = id
        info = nil
        Task { await prepareHost() }
    }

    /// The host's last project (or its most recent one) and agent info. A
    /// remembered folder that this host does not have (it came from another
    /// computer) is dropped.
    private func prepareHost() async {
        guard let conn else { return }
        error = nil
        var path = Prefs.lastProject(host: conn.id)
        info = await conn.info(for: agent)
        if let remembered = path, !(await conn.folderExists(remembered)) {
            path = nil
        }
        if path == nil {
            await conn.loadProjects()
            path = conn.projects.first?.path
        }
        cwd = path
    }

    private func create() {
        guard let cwd, let conn else {
            pickingProject = true
            return
        }
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty || !attachments.isEmpty else { return }
        creating = true
        error = nil
        Task {
            do {
                let summary = try await conn.createSession(
                    agent: agent, cwd: cwd, text: message, images: attachments, model: model, mode: mode)
                Prefs.setLastModel(model, agent)
                Prefs.setLastMode(mode, agent)
                text = ""
                Prefs.setDraft("", "new")
                creating = false
                onCreated(conn.id, summary.sid)
            } catch {
                creating = false
                self.error = error.localizedDescription
            }
        }
    }
}

struct ProjectPicker: View {
    @Environment(\.dismiss) private var dismiss
    let conn: HostConnection
    let selected: String?
    let choose: (String) -> Void
    @State private var query = ""
    @State private var checking = false
    @State private var missing: String?
    @State private var pathError: String?

    private var filtered: [ProjectEntry] {
        guard !query.isEmpty else { return conn.projects }
        return conn.projects.filter { $0.path.localizedCaseInsensitiveContains(query) }
    }

    private var typedPath: Bool { query.hasPrefix("/") || query.hasPrefix("~") }

    var body: some View {
        NavigationStack {
            List {
                if typedPath {
                    Section {
                        Button {
                            usePath(query)
                        } label: {
                            HStack {
                                Label("使用 \(query)", systemImage: "arrow.right.circle")
                                if checking {
                                    Spacer()
                                    ProgressView()
                                }
                            }
                        }
                        .disabled(checking)
                    } footer: {
                        if let pathError {
                            Text(pathError).foregroundStyle(.red)
                        }
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
                        FolderBrowser(conn: conn, path: conn.hello?.home ?? "~", canGoUp: true) { path in pick(path) }
                    } label: {
                        Label("浏览文件夹…", systemImage: "folder")
                    }
                }
            }
            .searchable(text: $query, prompt: "搜索，或输入路径")
            .onSubmit(of: .search) {
                if typedPath { usePath(query) }
            }
            .onChange(of: query) { pathError = nil }
            .navigationTitle("选择项目")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
            }
            .alert("新建文件夹？", isPresented: Binding(get: { missing != nil }, set: { if !$0 { missing = nil } })) {
                Button("取消", role: .cancel) {}
                Button("新建并使用") {
                    if let missing { createMissing(missing) }
                }
            } message: {
                Text("这台电脑上还没有 \(shortPath(missing ?? ""))。要新建它，用它开始会话吗？")
            }
            .task { await conn.loadProjects() }
        }
    }

    private func shortPath(_ path: String) -> String {
        conn.shortPath(path)
    }

    private func pick(_ path: String) {
        choose(conn.absolutePath(path))
        dismiss()
    }

    /// A typed path that exists is used; one that does not is offered as a
    /// new folder, so a typo never makes one without asking.
    private func usePath(_ typed: String) {
        let path = conn.absolutePath(typed)
        checking = true
        pathError = nil
        Task {
            let exists = await conn.folderExists(path)
            checking = false
            if exists {
                pick(path)
            } else {
                missing = path
            }
        }
    }

    private func createMissing(_ path: String) {
        let parent = (path as NSString).deletingLastPathComponent
        let name = (path as NSString).lastPathComponent
        checking = true
        Task {
            do {
                let made = try await conn.createFolder(in: parent, named: name)
                checking = false
                pick(made)
            } catch {
                checking = false
                pathError = error.localizedDescription
            }
        }
    }
}

/// One folder's subfolders. "新建文件夹" makes a folder here and picks it
/// right away: a new project starts empty, so there is nothing to browse into.
/// The first level (home) can also climb to its parent; deeper levels go up
/// with the back button.
struct FolderBrowser: View {
    let conn: HostConnection
    let path: String
    var canGoUp = false
    let choose: (String) -> Void
    @State private var climbed: String?
    @State private var listing: DirListing?
    @State private var error: String?
    @State private var naming = false
    @State private var newName = ""
    @State private var creating = false
    @State private var createError: String?

    private var shown: String { climbed ?? path }

    var body: some View {
        List {
            if let listing {
                Section {
                    Button {
                        choose(listing.path)
                    } label: {
                        Label("就用这个文件夹", systemImage: "checkmark.circle.fill")
                    }
                    Button {
                        startNaming()
                    } label: {
                        HStack {
                            Label("在这里新建文件夹…", systemImage: "folder.badge.plus")
                            if creating {
                                Spacer()
                                ProgressView()
                            }
                        }
                    }
                    .disabled(creating)
                } footer: {
                    if let createError {
                        Text(createError).foregroundStyle(.red)
                    }
                }
                Section {
                    if canGoUp, let parent = listing.parent {
                        Button {
                            climb(to: parent)
                        } label: {
                            Label("上一级 · \(conn.shortPath(parent))", systemImage: "arrow.up")
                        }
                    }
                    ForEach(listing.entries) { entry in
                        NavigationLink {
                            FolderBrowser(conn: conn, path: entry.path, choose: choose)
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
        .navigationTitle(Self.title(listing?.path ?? shown))
        .navigationBarTitleDisplayMode(.inline)
        .alert("新建文件夹", isPresented: $naming) {
            TextField("文件夹名", text: $newName)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("取消", role: .cancel) {}
            Button("新建并使用") { create() }
                .disabled(!Self.isValidName(newName))
        } message: {
            Text("在 \(conn.shortPath(listing?.path ?? shown)) 里新建，并用它开始会话。")
        }
        .task(id: shown) {
            do {
                listing = try await conn.call("dirs.list", ["path": .string(shown)])
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    static func title(_ path: String) -> String {
        path == "/" ? "/" : (path as NSString).lastPathComponent
    }

    static func isValidName(_ name: String) -> Bool {
        let name = name.trimmingCharacters(in: .whitespaces)
        return !name.isEmpty && !name.contains("/") && name != "." && name != ".."
    }

    private func climb(to parent: String) {
        listing = nil
        createError = nil
        climbed = parent
    }

    private func startNaming() {
        newName = ""
        createError = nil
        naming = true
    }

    private func create() {
        guard let parent = listing?.path, Self.isValidName(newName) else { return }
        let name = newName.trimmingCharacters(in: .whitespaces)
        creating = true
        createError = nil
        Task {
            do {
                let made = try await conn.createFolder(in: parent, named: name)
                creating = false
                choose(made)
            } catch {
                creating = false
                createError = error.localizedDescription
            }
        }
    }
}
