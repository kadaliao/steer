import Foundation
import Observation

enum LinkStatus: Equatable {
    case idle
    case connecting(String)
    case connected
    case failed(String)
}

/// A session together with the host it lives on.
struct HostedSession: Identifiable, Hashable {
    let hostID: UUID
    let summary: SessionSummary
    var id: String { hostID.uuidString + "/" + summary.sid }
}

/// One host: its SSH link to the helper, its sessions and what the helper
/// told us about the agents there. Every host stays connected on its own.
@MainActor
@Observable
final class HostConnection: Identifiable {
    var host: HostConfig
    var status: LinkStatus = .idle
    var hello: HelloInfo?
    var sessions: [SessionSummary] = []
    var projects: [ProjectEntry] = []
    var agentInfo: [String: AgentInfo] = [:]
    /// Whether this host's helper can push to this phone (has an APNs key and
    /// knows the device token).
    var pushReady: Bool?

    private(set) var chats: [String: ChatModel] = [:]
    @ObservationIgnored private var commandCache: [String: (at: Date, list: [CommandOption])] = [:]
    @ObservationIgnored private var link: HelperLink?
    @ObservationIgnored private var linkTasks: [Task<Void, Never>] = []
    @ObservationIgnored private var reconnectTask: Task<Void, Never>?
    @ObservationIgnored private var upgradeTask: Task<Void, Never>?
    @ObservationIgnored private weak var app: AppModel?

    nonisolated let id: UUID

    init(host: HostConfig, app: AppModel?) {
        self.id = host.id
        self.host = host
        self.app = app
    }
    var isConnected: Bool { status == .connected }
    var name: String { host.displayName }

    var hosted: [HostedSession] { sessions.map { HostedSession(hostID: host.id, summary: $0) } }

    // MARK: connection

    func connect() async {
        guard let app else { return }
        if case .connecting = status { return }
        if isConnected { return }
        reconnectTask?.cancel()
        status = .connecting(String(localized: "正在连接…"))
        let credential: Credential
        do {
            switch host.auth {
            case .deviceKey:
                credential = .key(try app.deviceKey())
            case .password:
                guard let data = try app.keychain.read(host.passwordAccount) else { throw LinkError.noPassword }
                credential = .password(String(decoding: data, as: UTF8.self))
            }
        } catch {
            status = .failed(error.localizedDescription)
            return
        }
        guard let script = AppModel.helperScript else {
            status = .failed(String(localized: "安装包里缺少主机助手。"))
            return
        }
        let link = HelperLink()
        do {
            let hello = try await link.start(
                host: host, credential: credential, helperScript: script.data, helperVersion: script.version,
                progress: { [weak self] text in await self?.setProgress(text) })
            self.link = link
            self.hello = hello
            status = .connected
            listen(to: link)
            if HelperLink.isVersion(hello.helper, olderThan: script.version) {
                upgradeWhenIdle()
            }
            await refreshSessions(watch: true)
            for chat in chats.values where chat.isVisible {
                await chat.load()
            }
            // Never let the permission prompt hold up the session list.
            Task { [weak self] in
                guard let self, let app = self.app else { return }
                if app.pushToken != nil {
                    await self.registerPush()
                } else {
                    await app.askForNotificationsIfNeeded()
                }
            }
        } catch let LinkError.needsTrust(fingerprint) {
            status = .idle
            app.askTrust(host, fingerprint)
        } catch {
            #if DEBUG
            print("STEER connect \(host.displayName) failed: \(error) cancelled=\(Task.isCancelled)")
            #endif
            await link.shutdown(reason: "start failed")
            status = .failed(error.localizedDescription)
            scheduleReconnect()
        }
    }

    private func setProgress(_ text: String) {
        if case .connecting = status { status = .connecting(text) }
    }

    func disconnect() {
        reconnectTask?.cancel()
        upgradeTask?.cancel()
        for task in linkTasks { task.cancel() }
        linkTasks = []
        if let link {
            Task { await link.shutdown(reason: "disconnect") }
        }
        link = nil
        status = .idle
    }

    /// Drops the link and connects again, e.g. after the host was edited.
    func reconnect() {
        disconnect()
        Task { await connect() }
    }

    private func listen(to link: HelperLink) {
        for task in linkTasks { task.cancel() }
        linkTasks = [
            Task { [weak self] in
                for await frame in link.events {
                    self?.handle(frame)
                }
            },
            Task { [weak self] in
                for await reason in link.closedSignal {
                    self?.linkClosed(link, reason: reason)
                }
            },
        ]
    }

    /// The helper running on the host is older than the one this app
    /// installed: it was busy when the new one arrived, so it stayed. Ask it
    /// to step down whenever nothing is running (it refuses while busy); the
    /// reconnect then starts the new one.
    private func upgradeWhenIdle() {
        upgradeTask?.cancel()
        upgradeTask = Task { [weak self] in
            var first = true
            while !Task.isCancelled {
                if !first { try? await Task.sleep(for: .seconds(30)) }
                first = false
                guard let self, self.isConnected, !Task.isCancelled else { return }
                if (try? await self.call("shutdown", timeout: .seconds(10)) as Empty) != nil { return }
            }
        }
    }

    private func linkClosed(_ closed: HelperLink, reason: String) {
        guard closed === link else { return }
        link = nil
        status = .failed(String(localized: "连接断开，正在重连…"))
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        reconnectTask?.cancel()
        guard app?.isActive == true else { return }
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, let self, self.app?.isActive == true, !self.isConnected else { return }
            if case .failed = self.status { self.status = .idle }
            // connect() cancels a pending retry; this one is no longer pending.
            self.reconnectTask = nil
            await self.connect()
        }
    }

    /// Back in the foreground: notice a link that died while suspended.
    func resume() async {
        if let link, await link.isClosed {
            self.link = nil
            status = .idle
        }
        if !isConnected { await connect() }
    }

    /// Runs a helper op on this host's link.
    func call<T: Decodable & Sendable>(_ op: String, _ params: [String: JSONValue] = [:],
                                       timeout: Duration = .seconds(60)) async throws -> T {
        guard let link else { throw LinkError.closed }
        return try await link.call(op, params, timeout: timeout)
    }

    // MARK: sessions

    func refreshSessions(watch: Bool = false) async {
        do {
            let list: SessionList = try await call(watch ? "watch" : "sessions.list", timeout: .seconds(45))
            sessions = list.sessions
            for summary in list.sessions {
                chats[summary.sid]?.summary = summary
            }
        } catch {
            // Keep what we had; the banner shows the link state.
        }
    }

    /// Gives the helper this phone's device token, tagged with this host's id
    /// so a tapped notification knows where to go.
    func registerPush() async {
        guard let token = app?.pushToken else { return }
        let result: PushRegistration? = try? await call("push.register", [
            "token": .string(token), "env": .string(AppModel.pushEnvironment), "host": .string(id.uuidString),
            "topic": .string(Bundle.main.bundleIdentifier ?? "com.kadaliao.steer"),
        ])
        pushReady = result?.configured
    }

    func sendTestPush() async throws -> PushTestResult {
        try await call("push.test", timeout: .seconds(30))
    }

    /// Whether a folder exists on this host.
    func folderExists(_ path: String) async -> Bool {
        (try? await call("dirs.list", ["path": .string(path)], timeout: .seconds(20)) as DirListing) != nil
    }

    func loadProjects() async {
        if let list: ProjectList = try? await call("projects.recent", timeout: .seconds(45)) {
            projects = list.projects
        }
    }

    func info(for agent: String) async -> AgentInfo? {
        if let cached = agentInfo[agent] { return cached }
        guard let info: AgentInfo = try? await call("agent.info", ["agent": .string(agent)], timeout: .seconds(45)) else {
            return nil
        }
        agentInfo[agent] = info
        return info
    }

    /// Commands and skills for a folder. Answers from the cache when it has
    /// one; asks the helper (which may start the agent briefly) otherwise.
    func cachedCommands(agent: String, cwd: String) -> [CommandOption]? {
        commandCache[agent + "|" + cwd]?.list
    }

    func commands(agent: String, cwd: String, reload: Bool = false) async throws -> [CommandOption] {
        let key = agent + "|" + cwd
        if !reload, let cached = commandCache[key], Date().timeIntervalSince(cached.at) < 300 {
            return cached.list
        }
        let list: CommandList = try await call(
            "agent.commands", ["agent": .string(agent), "cwd": .string(cwd), "reload": .bool(reload)],
            timeout: .seconds(45))
        commandCache[key] = (Date(), list.commands)
        return list.commands
    }

    private func handle(_ frame: EventFrame) {
        if frame.ev == "session", let summary = frame.session {
            if let index = sessions.firstIndex(where: { $0.sid == summary.sid }) {
                sessions[index] = summary
            } else {
                sessions.insert(summary, at: 0)
            }
            chats[summary.sid]?.summary = summary
            return
        }
        if let sid = frame.sid {
            chats[sid]?.apply(frame)
        }
    }

    func chat(for summary: SessionSummary) -> ChatModel {
        if let chat = chats[summary.sid] { return chat }
        let chat = ChatModel(summary: summary, conn: self)
        chats[summary.sid] = chat
        return chat
    }

    func chat(sid: String) -> ChatModel? {
        if let chat = chats[sid] { return chat }
        guard let summary = sessions.first(where: { $0.sid == sid }) else { return nil }
        return chat(for: summary)
    }

    func createSession(agent: String, cwd: String, text: String, images: [ImageAttachment],
                       model: String?, mode: String?) async throws -> SessionSummary {
        var params: [String: JSONValue] = [
            "agent": .string(agent), "cwd": .string(cwd), "text": .string(text),
            "images": .array(images.map(\.json)),
        ]
        if let model { params["model"] = .string(model) }
        if let mode { params["mode"] = .string(mode) }
        let result: CreateResult = try await call("session.create", params, timeout: .seconds(120))
        if !sessions.contains(where: { $0.sid == result.session.sid }) {
            sessions.insert(result.session, at: 0)
        }
        Prefs.setLastProject(cwd, host: host.id)
        Prefs.lastAgent = agent
        Prefs.lastHostID = host.id
        _ = chat(for: result.session)
        return result.session
    }

    func archive(_ summary: SessionSummary) async {
        sessions.removeAll { $0.sid == summary.sid }
        let _: Empty? = try? await call("session.archive", [
            "sid": .string(summary.sid), "agent": .string(summary.agent), "cwd": .string(summary.cwd),
        ])
    }

    func markSeen(_ summary: SessionSummary) {
        app?.markSeen(summary)
    }

    /// Shortens a path under the host's home folder to `~/…`.
    func shortPath(_ path: String) -> String {
        guard let home = hello?.home, path.hasPrefix(home) else { return path }
        return "~" + path.dropFirst(home.count)
    }
}
