import Foundation

enum AuthMethod: String, Codable, Sendable, CaseIterable {
    case deviceKey
    case password
}

/// A machine the app drives over SSH.
struct HostConfig: Codable, Sendable, Identifiable, Hashable {
    var id = UUID()
    var name: String
    var address: String
    var port: Int = 22
    var username: String
    var auth: AuthMethod = .deviceKey
    /// Trusted host key fingerprint (trust on first use).
    var fingerprint: String?

    var displayName: String { name.isEmpty ? address : name }
    var passwordAccount: String { "password-" + id.uuidString }
}

/// Small persisted preferences: hosts, drafts, last choices.
@MainActor
enum Prefs {
    private static let defaults = UserDefaults.standard

    static var hosts: [HostConfig] {
        get { decode([HostConfig].self, "hosts") ?? [] }
        set { encode(newValue, "hosts") }
    }

    /// The host the home list is narrowed to; nil shows every host.
    static var homeScope: UUID? {
        get { defaults.string(forKey: "homeScope").flatMap(UUID.init(uuidString:)) }
        set { defaults.set(newValue?.uuidString, forKey: "homeScope") }
    }

    /// The host the last new session was started on.
    static var lastHostID: UUID? {
        get { defaults.string(forKey: "lastHost").flatMap(UUID.init(uuidString:)) }
        set { defaults.set(newValue?.uuidString, forKey: "lastHost") }
    }

    static var returnSends: Bool {
        get { defaults.bool(forKey: "returnSends") }
        set { defaults.set(newValue, forKey: "returnSends") }
    }

    static func draft(_ key: String) -> String {
        defaults.string(forKey: "draft." + key) ?? ""
    }

    static func setDraft(_ text: String, _ key: String) {
        if text.isEmpty {
            defaults.removeObject(forKey: "draft." + key)
        } else {
            defaults.set(text, forKey: "draft." + key)
        }
    }

    /// The last project used on a host. `lastProject` alone is the fallback
    /// (and what UI tests set).
    static func lastProject(host: UUID) -> String? {
        defaults.string(forKey: "lastProject." + host.uuidString) ?? defaults.string(forKey: "lastProject")
    }

    static func setLastProject(_ path: String, host: UUID) {
        defaults.set(path, forKey: "lastProject." + host.uuidString)
    }

    static var lastAgent: String {
        get { defaults.string(forKey: "lastAgent") ?? "claude" }
        set { defaults.set(newValue, forKey: "lastAgent") }
    }

    static func lastModel(_ agent: String) -> String? {
        defaults.string(forKey: "lastModel." + agent)
    }

    static func setLastModel(_ model: String?, _ agent: String) {
        defaults.set(model, forKey: "lastModel." + agent)
    }

    static func lastMode(_ agent: String) -> String? {
        defaults.string(forKey: "lastMode." + agent)
    }

    static func setLastMode(_ mode: String?, _ agent: String) {
        defaults.set(mode, forKey: "lastMode." + agent)
    }

    static var seen: [String: Double] {
        get { (defaults.dictionary(forKey: "seen") as? [String: Double]) ?? [:] }
        set { defaults.set(newValue, forKey: "seen") }
    }

    private static func decode<T: Decodable>(_ type: T.Type, _ key: String) -> T? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    private static func encode<T: Encodable>(_ value: T, _ key: String) {
        defaults.set(try? JSONEncoder().encode(value), forKey: key)
    }
}
