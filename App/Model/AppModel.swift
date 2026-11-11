import Foundation
import Observation
import SwiftUI

/// App-wide state: the hosts, one live connection per host, and which hosts
/// the home list shows.
@MainActor
@Observable
final class AppModel {
    var hosts: [HostConfig] = Prefs.hosts
    var connections: [UUID: HostConnection] = [:]
    /// The host the home list is narrowed to; nil shows every host.
    var scope: UUID? = Prefs.homeScope {
        didSet { Prefs.homeScope = scope }
    }
    var trustQueue: [(host: HostConfig, fingerprint: String)] = []
    var seen: [String: Double] = Prefs.seen
    var isActive = true
    let keychain = Keychain(service: "com.kadaliao.steer")

    init() {
        for host in hosts {
            connections[host.id] = HostConnection(host: host, app: self)
        }
    }

    /// Connections in the order the hosts were added.
    var ordered: [HostConnection] { hosts.compactMap { connections[$0.id] } }

    func connection(_ id: UUID?) -> HostConnection? {
        guard let id else { return nil }
        return connections[id]
    }

    /// Connections the home list shows.
    var inScope: [HostConnection] {
        if let scoped = connection(scope) { return [scoped] }
        return ordered
    }

    var hasManyHosts: Bool { hosts.count > 1 }

    /// Every session in scope, newest first.
    var sessions: [HostedSession] {
        inScope.flatMap(\.hosted).sorted { ($0.summary.updatedAt ?? 0) > ($1.summary.updatedAt ?? 0) }
    }

    /// Where a new session starts: the scoped host, else the last one used,
    /// else the first connected one.
    var hostForNewChat: HostConnection? {
        if let scoped = connection(scope) { return scoped }
        if let last = connection(Prefs.lastHostID), last.isConnected { return last }
        return ordered.first { $0.isConnected } ?? ordered.first
    }

    var anyConnected: Bool { inScope.contains { $0.isConnected } }

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
        if let existing = connections[host.id] {
            existing.host = host
        } else {
            connections[host.id] = HostConnection(host: host, app: self)
        }
    }

    func deleteHost(_ host: HostConfig) {
        hosts.removeAll { $0.id == host.id }
        try? keychain.remove(host.passwordAccount)
        Prefs.hosts = hosts
        connections[host.id]?.disconnect()
        connections[host.id] = nil
        if scope == host.id { scope = nil }
    }

    func deviceKey() throws -> DeviceKey {
        try DeviceKey.loadOrCreate(in: keychain)
    }

    func askTrust(_ host: HostConfig, _ fingerprint: String) {
        if !trustQueue.contains(where: { $0.host.id == host.id }) {
            trustQueue.append((host, fingerprint))
        }
        #if DEBUG
        if DebugLaunch.trustAnyHost { trustPending() }
        #endif
    }

    func trustPending() {
        guard !trustQueue.isEmpty else { return }
        let pending = trustQueue.removeFirst()
        var host = pending.host
        host.fingerprint = pending.fingerprint
        saveHost(host, password: nil)
        Task { await connections[host.id]?.connect() }
    }

    func declineTrust() {
        guard !trustQueue.isEmpty else { return }
        trustQueue.removeFirst()
    }

    // MARK: connection

    func connectAll() async {
        await withTaskGroup(of: Void.self) { group in
            for conn in ordered {
                group.addTask { await conn.connect() }
            }
        }
    }

    func refreshAll() async {
        await withTaskGroup(of: Void.self) { group in
            for conn in inScope {
                group.addTask { await conn.refreshSessions() }
            }
        }
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        isActive = phase == .active
        guard phase == .active else { return }
        for conn in ordered {
            Task { await conn.resume() }
        }
    }

    // MARK: unread

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
