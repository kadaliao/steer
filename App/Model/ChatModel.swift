import Foundation
import Observation
import UIKit

/// An image picked or pasted for the next message, already downscaled.
struct ImageAttachment: Identifiable, Sendable, Equatable {
    let id = UUID()
    let data: Data
    let mime: String

    var json: JSONValue {
        .object(["mime": .string(mime), "data": .string(data.base64EncodedString())])
    }

    /// Downscales to at most 1600 px on the long side, JPEG.
    static func make(from data: Data) -> ImageAttachment? {
        guard let image = UIImage(data: data) else { return nil }
        let longest = max(image.size.width, image.size.height)
        let scale = min(1, 1600 / max(longest, 1))
        let size = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let resized = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        guard let jpeg = resized.jpegData(compressionQuality: 0.8) else { return nil }
        return ImageAttachment(data: jpeg, mime: "image/jpeg")
    }
}

/// What the composer's send button does next.
enum ComposerIntent: Equatable {
    case message
    /// Denying a request with a note that tells the agent what to do instead.
    case denyWithNote(AgentRequest)
    /// Answering a question in free text.
    case answer(AgentRequest, questionID: String)
}

/// One open conversation.
@MainActor
@Observable
final class ChatModel {
    let sid: String
    var summary: SessionSummary
    var items: [TimelineItem] = []
    var requests: [AgentRequest] = []
    var earlier = 0
    var loaded = false
    var loading = false
    var loadError: String?
    var actionError: String?
    var isVisible = false
    var intent: ComposerIntent = .message
    /// Answers collected so far for a multi-question request.
    var partialAnswers: [String: [String: JSONValue]] = [:]
    private var index: [String: Int] = [:]
    @ObservationIgnored private weak var app: AppModel?

    init(summary: SessionSummary, app: AppModel) {
        self.sid = summary.sid
        self.summary = summary
        self.app = app
    }

    var isRunning: Bool { summary.isRunning }
    var agent: String { summary.agent }

    func load() async {
        guard let app else { return }
        loading = true
        defer { loading = false }
        do {
            let snapshot: Snapshot = try await app.call("session.open", [
                "sid": .string(sid), "agent": .string(summary.agent), "cwd": .string(summary.cwd),
                "title": .string(summary.title),
            ], timeout: .seconds(60))
            summary = snapshot.session
            items = snapshot.items
            requests = snapshot.requests
            earlier = snapshot.earlier ?? 0
            rebuildIndex()
            loaded = true
            loadError = nil
        } catch {
            loadError = error.localizedDescription
        }
    }

    func close() {
        isVisible = false
        guard let app else { return }
        Task { let _: Empty? = try? await app.call("session.close", ["sid": .string(sid)]) }
    }

    private func rebuildIndex() {
        index = [:]
        for (i, item) in items.enumerated() { index[item.id] = i }
    }

    func apply(_ frame: EventFrame) {
        switch frame.ev {
        case "item":
            guard let item = frame.item else { return }
            if let i = index[item.id] {
                items[i] = item
            } else {
                index[item.id] = items.count
                items.append(item)
            }
        case "delta":
            guard let id = frame.itemId, let i = index[id], let text = frame.text else { return }
            items[i].text = (items[i].text ?? "") + text
            items[i].streaming = true
        case "request":
            guard let request = frame.request else { return }
            if let i = requests.firstIndex(where: { $0.rid == request.rid }) {
                requests[i] = request
            } else {
                requests.append(request)
            }
        case "resolved":
            requests.removeAll { $0.rid == frame.rid }
            if case .denyWithNote(let r) = intent, r.rid == frame.rid { intent = .message }
            if case .answer(let r, _) = intent, r.rid == frame.rid { intent = .message }
        default:
            break
        }
    }

    // MARK: actions

    func send(_ text: String, images: [ImageAttachment]) async -> Bool {
        guard let app else { return false }
        switch intent {
        case .denyWithNote(let request):
            intent = .message
            return await respond(request, decision: "deny", message: text)
        case .answer(let request, let questionID):
            intent = .message
            return await answer(request, questionID: questionID, values: [text])
        case .message:
            break
        }
        do {
            let _: Empty = try await app.call("session.send", [
                "sid": .string(sid), "text": .string(text), "images": .array(images.map(\.json)),
            ], timeout: .seconds(120))
            actionError = nil
            return true
        } catch {
            actionError = error.localizedDescription
            return false
        }
    }

    @discardableResult
    func respond(_ request: AgentRequest, decision: String, message: String = "",
                 answers: [String: JSONValue] = [:]) async -> Bool {
        guard let app else { return false }
        do {
            let _: Empty = try await app.call("session.respond", [
                "sid": .string(sid), "rid": .string(request.rid), "decision": .string(decision),
                "message": .string(message), "answers": .object(answers),
            ])
            requests.removeAll { $0.rid == request.rid }
            partialAnswers[request.rid] = nil
            actionError = nil
            return true
        } catch {
            actionError = error.localizedDescription
            return false
        }
    }

    /// Records one question's answer; submits when every question has one.
    @discardableResult
    func answer(_ request: AgentRequest, questionID: String, values: [String]) async -> Bool {
        var answers = partialAnswers[request.rid] ?? [:]
        answers[questionID] = values.count == 1 ? .string(values[0]) : .array(values.map(JSONValue.string))
        partialAnswers[request.rid] = answers
        let total = request.questions?.count ?? 1
        if answers.count >= total {
            return await respond(request, decision: "allow", answers: answers)
        }
        return true
    }

    func interrupt() async {
        guard let app else { return }
        let _: Empty? = try? await app.call("session.interrupt", ["sid": .string(sid)])
    }

    func setMode(_ mode: String) async {
        guard let app else { return }
        if let summary: SessionSummary = try? await app.call("session.setMode", ["sid": .string(sid), "mode": .string(mode)]) {
            self.summary = summary
        }
        Prefs.setLastMode(mode, agent)
    }

    func setModel(_ model: String) async {
        guard let app else { return }
        if let summary: SessionSummary = try? await app.call("session.setModel", ["sid": .string(sid), "model": .string(model)]) {
            self.summary = summary
        }
        Prefs.setLastModel(model, agent)
    }

    func rename(_ title: String) async {
        guard let app else { return }
        if let summary: SessionSummary = try? await app.call("session.rename", ["sid": .string(sid), "title": .string(title)]) {
            self.summary = summary
        }
    }

    func searchFiles(_ query: String) async -> [String] {
        guard let app else { return [] }
        let list: FileList? = try? await app.call("files.search", [
            "cwd": .string(summary.cwd), "query": .string(query), "limit": 30,
        ])
        return list?.files ?? []
    }
}
