import SwiftUI

/// Cards for what the agent needs from you, docked right above the composer
/// so the answer is one thumb tap away.
struct RequestCard: View {
    let request: AgentRequest
    let chat: ChatModel
    var focusComposer: () -> Void

    var body: some View {
        Group {
            switch request.kind {
            case "question": QuestionCard(request: request, chat: chat, focusComposer: focusComposer)
            case "plan": PlanCard(request: request, chat: chat, focusComposer: focusComposer)
            default: PermissionCard(request: request, chat: chat, focusComposer: focusComposer)
            }
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 20)
                .fill(Color(.systemBackground))
                .shadow(color: .black.opacity(0.12), radius: 12, y: 2)
        )
        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(Color.needsYou.opacity(0.35)))
    }
}

private struct CardButton: View {
    let title: String
    var prominent = false
    var role: ButtonRole?
    var action: () -> Void

    var body: some View {
        if prominent {
            Button(role: role, action: action) {
                Text(title).font(.body.weight(.semibold)).frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.borderedProminent)
            .buttonBorderShape(.roundedRectangle(radius: 14))
        } else {
            Button(role: role, action: action) {
                Text(title)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(role == .destructive ? Color.red : Color.primary)
                    .frame(maxWidth: .infinity, minHeight: 42)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
            }
            .buttonStyle(.plain)
        }
    }
}

struct PermissionCard: View {
    let request: AgentRequest
    let chat: ChatModel
    var focusComposer: () -> Void
    @State private var expanded = false
    @State private var confirmDeny = false
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: ToolText.icon(request.category)).foregroundStyle(Color.needsYou)
                Text(ToolText.requestTitle(request)).font(.headline)
                Spacer()
                if request.add != nil || request.del != nil {
                    DiffCounts(add: request.add, del: request.del)
                }
            }
            if let target = request.target, !target.isEmpty {
                Text(target)
                    .font(.system(.callout, design: .monospaced))
                    .lineLimit(expanded ? 40 : 4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color(.secondarySystemBackground)))
                    .onTapGesture { expanded.toggle() }
            }
            if let reason = request.reason ?? request.detail, !reason.isEmpty, reason != request.target {
                Text(reason).font(.footnote).foregroundStyle(.secondary).lineLimit(3)
            }
            if let diff = request.diff, !diff.isEmpty {
                DisclosureGroup("查看改动") {
                    ScrollView {
                        DiffView(diff: diff)
                    }
                    .frame(maxHeight: 220)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                .font(.footnote)
            }
            CardButton(title: String(localized: "允许"), prominent: true) { decide("allow") }
            HStack(spacing: 10) {
                if request.canAllowSession != false {
                    CardButton(title: String(localized: "本会话都允许")) { decide("allowSession") }
                }
                CardButton(title: String(localized: "拒绝…"), role: .destructive) { confirmDeny = true }
            }
        }
        .disabled(busy)
        .confirmationDialog("拒绝这一步", isPresented: $confirmDeny, titleVisibility: .visible) {
            Button("拒绝并停下", role: .destructive) { decide("deny") }
            Button("拒绝，告诉它换个做法") {
                chat.intent = .denyWithNote(request)
                focusComposer()
            }
        }
    }

    private func decide(_ decision: String) {
        busy = true
        Task {
            await chat.respond(request, decision: decision)
            busy = false
        }
    }
}

struct QuestionCard: View {
    let request: AgentRequest
    let chat: ChatModel
    var focusComposer: () -> Void
    @State private var selected: Set<String> = []

    private var question: RequestQuestion? {
        let answered = chat.partialAnswers[request.rid] ?? [:]
        return request.questions?.first { answered[$0.id] == nil }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let question {
                HStack {
                    Image(systemName: "questionmark.bubble").foregroundStyle(Color.needsYou)
                    Text(question.header?.isEmpty == false ? question.header ?? "" : String(localized: "它在问你"))
                        .font(.headline)
                    Spacer()
                    if let total = request.questions?.count, total > 1 {
                        Text("\((chat.partialAnswers[request.rid]?.count ?? 0) + 1)/\(total)")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Text(question.question).font(.callout)
                ForEach(question.options, id: \.label) { option in
                    Button {
                        if question.multiSelect == true {
                            if selected.contains(option.label) { selected.remove(option.label) } else { selected.insert(option.label) }
                        } else {
                            submit(question, [option.label])
                        }
                    } label: {
                        HStack(alignment: .firstTextBaseline) {
                            if question.multiSelect == true {
                                Image(systemName: selected.contains(option.label) ? "checkmark.square.fill" : "square")
                            }
                            VStack(alignment: .leading, spacing: 2) {
                                Text(option.label).font(.body.weight(.medium))
                                if let description = option.description, !description.isEmpty {
                                    Text(description).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.leading)
                                }
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
                    }
                    .buttonStyle(.plain)
                }
                HStack(spacing: 10) {
                    if question.multiSelect == true {
                        CardButton(title: String(localized: "确定"), prominent: true) {
                            submit(question, Array(selected))
                        }
                        .disabled(selected.isEmpty)
                    }
                    if question.allowOther != false {
                        CardButton(title: String(localized: "自己写…")) {
                            chat.intent = .answer(request, questionID: question.id)
                            focusComposer()
                        }
                    }
                }
            }
        }
    }

    private func submit(_ question: RequestQuestion, _ values: [String]) {
        selected = []
        Task { await chat.answer(request, questionID: question.id, values: values) }
    }
}

struct PlanCard: View {
    let request: AgentRequest
    let chat: ChatModel
    var focusComposer: () -> Void
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: "list.bullet.clipboard").foregroundStyle(Color.needsYou)
                Text("计划好了，要开始吗？").font(.headline)
            }
            ScrollView {
                MarkdownText(text: request.plan ?? "")
                    .font(.callout)
            }
            .frame(maxHeight: 260)
            CardButton(title: String(localized: "开始执行"), prominent: true) { decide("allow") }
            HStack(spacing: 10) {
                CardButton(title: String(localized: "执行并自动改文件")) { decide("allowSession") }
                CardButton(title: String(localized: "改计划…")) {
                    chat.intent = .denyWithNote(request)
                    focusComposer()
                }
            }
        }
        .disabled(busy)
    }

    private func decide(_ decision: String) {
        busy = true
        Task {
            await chat.respond(request, decision: decision)
            busy = false
        }
    }
}
