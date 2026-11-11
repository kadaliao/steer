import Foundation
import Observation
import SwiftUI

enum LinkStatus: Equatable {
    case idle
    case connecting(String)
    case connected
    case failed(String)
}

/// App-wide state: hosts, the live link to the active host, and the session list.
@MainActor
@Observable
final class AppModel {
    var hosts: [HostConfig] = Prefs.hosts
    var activeHostID: UUID? = Prefs.activeHostID
    var status: LinkStatus = .idle
    var hello: HelloInfo?
    var sessions: [SessionSummary] = []
    var projects: [ProjectEntry] = []
    var pendingTrust: (host: HostConfig, fingerprint: String)?
    var seen: [String: Double] = Prefs.seen
    var agentInfo: [String: AgentInfo] = [:]

    private(set) var chats: [String: ChatModel] = [:]
    private var link: HelperLink?
    private var linkTasks: [Task<Void, Never>] = []
    private var reconnectTask: Task<Void, Never>?
    private var isActive = true
    let keychain = Keychain(service: "com.kadaliao.steer")

    var activeHost: HostConfig? {
        hosts.first { $0.id == activeHostID } ?? hosts.first
    }

    var isConnected: Bool { status == .connected }

    // MARK: hosts

    func saveHost(_ host: HostConfig, password: String?) {
        if let index = hosts.firstIndex(where: { $0.id == host.id }) {
            hosts[index] = host
        } else {
            hosts.append(host)
        }
        if let password, !password.isEmpty {
            try? keychain.write(Data(password.utf8), host.passwordAccount)
        }
        Prefs.hosts = hosts
        if activeHostID == nil || activeHostID == host.id {
            activeHostID = host.id
            Prefs.activeHostID = host.id
        }
    }

    func deleteHost(_ host: HostConfig) {
        hosts.removeAll { $0.id == host.id }
        try? keychain.remove(host.passwordAccount)
        Prefs.hosts = hosts
        if activeHostID == host.id {
            activeHostID = hosts.first?.id
            Prefs.activeHostID = activeHostID
            disconnect()
        }
    }

    func switchHost(_ host: HostConfig) {
        guard host.id != activeHost?.id else { return }
        activeHostID = host.id
        Prefs.activeHostID = host.id
        disconnect()
        sessions = []
        chats = [:]
        Task { await connect() }
    }

    func deviceKey() throws -> DeviceKey {
        try DeviceKey.loadOrCreate(in: keychain)
    }

    func trustPending() {
        guard let pending = pendingTrust else { return }
        var host = pending.host
        host.fingerprint = pending.fingerprint
        saveHost(host, password: nil)
        pendingTrust = nil
        Task { await connect() }
    }

    // MARK: connection

    func connect() async {
        guard let host = activeHost else { return }
        if case .connecting = status { return }
        if isConnected { return }
        reconnectTask?.cancel()
        status = .connecting(String(localized: "正在连接…"))
        let credential: Credential
        do {
            switch host.auth {
            case .deviceKey:
                credential = .key(try deviceKey())
            case .password:
                guard let data = try keychain.read(host.passwordAccount) else { throw LinkError.noPassword }
                credential = .password(String(decoding: data, as: UTF8.self))
            }
        } catch {
            status = .failed(error.localizedDescription)
            return
        }
        guard let script = Self.helperScript else {
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
            await refreshSessions(watch: true)
            for chat in chats.values where chat.isVisible {
                await chat.load()
            }
        } catch let LinkError.needsTrust(fingerprint) {
            status = .idle
            pendingTrust = (host, fingerprint)
            #if DEBUG
            if DebugLaunch.trustAnyHost { trustPending() }
            #endif
        } catch {
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
        for task in linkTasks { task.cancel() }
        linkTasks = []
        if let link {
            Task { await link.shutdown(reason: "disconnect") }
        }
        link = nil
        status = .idle
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

    private func linkClosed(_ closed: HelperLink, reason: String) {
        guard closed === link else { return }
        link = nil
        status = .failed(String(localized: "连接断开，正在重连…"))
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        reconnectTask?.cancel()
        guard isActive, activeHost != nil else { return }
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, let self, self.isActive, !self.isConnected else { return }
            if case .failed = self.status { self.status = .idle }
            await self.connect()
        }
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        isActive = phase == .active
        if phase == .active {
            Task {
                if let link, await link.isClosed {
                    self.link = nil
                    self.status = .idle
                }
                if !self.isConnected { await self.connect() }
            }
        }
    }

    /// Runs a helper op on the live link.
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

    func loadProjects() async {
        if let list: ProjectList = try? await call("projects.recent", timeout: .seconds(45)) {
            projects = list.projects
        }
    }

    func info(for agent: String) async -> AgentInfo? {
        if let cached = agentInfo[agent] { return cached }
        guard let info: AgentInfo = try? await call("agent.info", ["agent": .string(agent)], timeout: .seconds(30)) else {
            return nil
        }
        agentInfo[agent] = info
        return info
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
        let chat = ChatModel(summary: summary, app: self)
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
        Prefs.lastProject = cwd
        Prefs.lastAgent = agent
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
        seen[summary.sid] = summary.updatedAt ?? Date().timeIntervalSince1970 * 1000
        Prefs.seen = seen
    }

    func isUnread(_ summary: SessionSummary) -> Bool {
        guard let updated = summary.updatedAt, !summary.isDesktop else { return false }
        return updated > (seen[summary.sid] ?? 0) + 1
    }

    // MARK: bundled helper

    struct HelperScript: Sendable {
        let data: Data
        let version: String
    }

    static let helperScript: HelperScript? = {
        guard let url = Bundle.main.url(forResource: "steerd", withExtension: "py"),
            let data = try? Data(contentsOf: url)
        else { return nil }
        let text = String(decoding: data, as: UTF8.self)
        let version = text.split(separator: "\n")
            .first { $0.hasPrefix("VERSION = ") }
            .map { $0.dropFirst("VERSION = ".count).trimmingCharacters(in: CharacterSet(charactersIn: "\" ")) }
        return HelperScript(data: data, version: version ?? "0")
    }()
}
