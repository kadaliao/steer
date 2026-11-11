import Foundation

// Types the host helper (helper/steerd.py) sends. Every field the helper
// may omit is optional so a newer helper never breaks an older app.

struct SessionSummary: Codable, Sendable, Identifiable, Equatable, Hashable {
    var sid: String
    var agent: String
    var cwd: String
    var title: String
    var state: String
    var needs: String?
    var lastText: String?
    var activity: Activity?
    var updatedAt: Double?
    var runningSince: Double?
    var model: String?
    var mode: String?
    var source: String?
    var live: Bool?
    var error: String?

    var id: String { sid }

    struct Activity: Codable, Sendable, Equatable, Hashable {
        var category: String?
        var target: String?
    }

    var isRunning: Bool { state == "running" }
    var isWaiting: Bool { state == "waiting" || needs != nil }
    var isDesktop: Bool { source == "desktop" }
    var projectName: String { (cwd as NSString).lastPathComponent }
    var updatedDate: Date? { updatedAt.map { Date(timeIntervalSince1970: $0 / 1000) } }
}

struct PlanStep: Codable, Sendable, Equatable, Hashable {
    var step: String
    var status: String
}

/// One row of a session's timeline: a user or agent message, a tool call,
/// a plan, a notice, or the end of a turn.
struct TimelineItem: Codable, Sendable, Identifiable, Equatable {
    var id: String
    var kind: String
    var text: String?
    var images: Int?
    var queued: Bool?
    var failed: Bool?
    var streaming: Bool?
    var phase: String?
    var tool: String?
    var category: String?
    var target: String?
    var detail: String?
    var command: String?
    var status: String?
    var output: String?
    var diff: String?
    var add: Int?
    var del: Int?
    var exitCode: Int?
    var steps: [PlanStep]?
    var code: String?
    var durationMs: Double?
    var costUsd: Double?
    var ts: Double?
    var endTs: Double?

    var isRunning: Bool { status == "running" }
}

struct QuestionOption: Codable, Sendable, Equatable, Hashable {
    var label: String
    var description: String?
}

struct RequestQuestion: Codable, Sendable, Equatable, Hashable, Identifiable {
    var id: String
    var header: String?
    var question: String
    var options: [QuestionOption]
    var multiSelect: Bool?
    var allowOther: Bool?
    var secret: Bool?
}

/// Something the agent needs from the user: a permission, an answer, or a
/// plan approval.
struct AgentRequest: Codable, Sendable, Equatable, Identifiable {
    var rid: String
    var kind: String
    var tool: String?
    var category: String?
    var target: String?
    var detail: String?
    var reason: String?
    var diff: String?
    var add: Int?
    var del: Int?
    var canAllowSession: Bool?
    var questions: [RequestQuestion]?
    var plan: String?

    var id: String { rid }
}

struct Snapshot: Decodable, Sendable {
    var session: SessionSummary
    var items: [TimelineItem]
    var earlier: Int?
    var requests: [AgentRequest]
}

struct AgentPresence: Decodable, Sendable, Equatable {
    var path: String
    var version: String?
}

struct HelloInfo: Decodable, Sendable, Equatable {
    var helper: String
    var proto: Int
    var home: String
    var host: String
    var platform: String
    var python: String?
    var launchd: String?
    var agents: [String: AgentPresence?]

    func has(_ agent: String) -> Bool {
        (agents[agent] ?? nil) != nil
    }
}

struct SessionList: Decodable, Sendable {
    var sessions: [SessionSummary]
}

struct ProjectEntry: Decodable, Sendable, Identifiable, Hashable {
    var path: String
    var name: String
    var updatedAt: Double?
    var agents: [String]?
    var git: Bool?
    var id: String { path }
}

struct ProjectList: Decodable, Sendable {
    var projects: [ProjectEntry]
}

struct DirEntry: Decodable, Sendable, Identifiable, Hashable {
    var name: String
    var path: String
    var git: Bool?
    var id: String { path }
}

struct DirListing: Decodable, Sendable {
    var path: String
    var parent: String?
    var entries: [DirEntry]
    var git: Bool?
}

struct ModelOption: Decodable, Sendable, Identifiable, Hashable {
    var id: String
    var name: String?
    var description: String?
    var `default`: Bool?
}

struct CommandOption: Decodable, Sendable, Identifiable, Hashable {
    var name: String
    var description: String?
    var argumentHint: String?
    var id: String { name }
}

struct AgentInfo: Decodable, Sendable {
    var models: [ModelOption]
    var commands: [CommandOption]
    var modes: [String]
}

struct CreateResult: Decodable, Sendable {
    var session: SessionSummary
}

struct FileList: Decodable, Sendable {
    var files: [String]
}

struct Empty: Decodable, Sendable {}

/// A pushed event: `ev` names the kind; the other fields depend on it.
struct EventFrame: Decodable, Sendable {
    var ev: String
    var sid: String?
    var item: TimelineItem?
    var session: SessionSummary?
    var request: AgentRequest?
    var rid: String?
    var itemId: String?
    var text: String?
}
