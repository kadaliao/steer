import PhotosUI
import SwiftUI
import UIKit

/// The message box. Uses the system keyboard (and so any input method and
/// dictation), grows with the text, and can expand to a full-screen editor.
struct Composer<Chips: View>: View {
    let conn: HostConnection?
    @Binding var text: String
    @Binding var attachments: [ImageAttachment]
    var focused: FocusState<Bool>.Binding
    let agent: String
    let cwd: String?
    let placeholder: String
    let isRunning: Bool
    /// The agent's guess at the next message; one tap sends it.
    var replySuggestion: String? = nil
    let canSend: Bool
    let onSend: () -> Void
    let onStop: () -> Void
    var onSendSuggestion: (String) -> Void = { _ in }
    @ViewBuilder let chips: () -> Chips

    @State private var pickerItems: [PhotosPickerItem] = []
    @State private var showPhotoPicker = false
    @State private var showEditor = false
    @State private var fileSuggestions: [String] = []
    @State private var commands: [CommandOption]?
    @State private var commandsError: String?
    /// Where the token was when its list was closed; it stays closed until
    /// a new `@`, `/` or `$` starts.
    @State private var dismissedAt: Int?

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var hasContent: Bool { !trimmed.isEmpty || !attachments.isEmpty }

    var body: some View {
        VStack(spacing: 8) {
            if focused.wrappedValue, let trigger, trigger.start != dismissedAt {
                suggestionPanel(trigger)
            } else if let replySuggestion, !replySuggestion.isEmpty, text.isEmpty, attachments.isEmpty, !isRunning {
                ReplySuggestionRow(text: replySuggestion, enabled: canSend) {
                    onSendSuggestion(replySuggestion)
                } edit: {
                    text = replySuggestion
                    focused.wrappedValue = true
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                if !attachments.isEmpty { attachmentStrip }
                TextField(placeholder, text: $text, axis: .vertical)
                    .lineLimit(1...9)
                    .font(.body)
                    .focused(focused)
                    .accessibilityIdentifier("composer")
                    .submitLabel(Prefs.returnSends ? .send : .return)
                    .onSubmit { if Prefs.returnSends, hasContent { onSend() } }
                    .padding(.top, 2)
                HStack(spacing: 8) {
                    attachMenu
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) { chips() }
                    }
                    if text.count > 120 || text.contains("\n") {
                        Button { showEditor = true } label: {
                            Image(systemName: "arrow.up.left.and.arrow.down.right").font(.footnote.weight(.semibold))
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("全屏编辑")
                    }
                    if focused.wrappedValue {
                        Button { focused.wrappedValue = false } label: {
                            Image(systemName: "keyboard.chevron.compact.down")
                                .font(.system(size: 15, weight: .medium))
                                .frame(width: 30, height: 34)
                        }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("收起键盘")
                        .accessibilityIdentifier("hideKeyboard")
                    }
                    sendButton
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)
            .padding(.bottom, 8)
            .background(RoundedRectangle(cornerRadius: 24).fill(Color(.secondarySystemBackground)))
        }
        .onChange(of: pickerItems) { _, items in loadPicked(items) }
        .onChange(of: trigger?.start) { _, start in
            if start == nil { dismissedAt = nil }
        }
        .task(id: fileQuery) { await loadFiles() }
        .task(id: commandKey) { await loadCommands() }
        .sheet(isPresented: $showEditor) {
            FullScreenEditor(text: $text, placeholder: placeholder)
        }
    }

    // MARK: triggers

    /// What the last word being typed asks for: a file after `@`, a command
    /// or skill after a leading `/`, a Codex skill after `$`.
    private struct Trigger: Equatable {
        enum Kind { case file, command, skill }
        let kind: Kind
        let query: String
        /// Offset of the trigger character, which identifies the token.
        let start: Int
    }

    private var trigger: Trigger? {
        let tokenStart = text.lastIndex(where: { $0 == " " || $0 == "\n" }).map { text.index(after: $0) } ?? text.startIndex
        let token = text[tokenStart...]
        guard let first = token.first else { return nil }
        let start = text.distance(from: text.startIndex, to: tokenStart)
        let query = String(token.dropFirst())
        switch first {
        case "@": return Trigger(kind: .file, query: query, start: start)
        case "/" where start == 0: return Trigger(kind: .command, query: query, start: start)
        case "$" where agent == "codex": return Trigger(kind: .skill, query: query, start: start)
        default: return nil
        }
    }

    private var fileQuery: String? {
        guard let trigger, trigger.kind == .file, trigger.start != dismissedAt else { return nil }
        return trigger.query
    }

    private var commandKey: String? {
        guard let trigger, trigger.kind != .file, let cwd else { return nil }
        return agent + "|" + cwd
    }

    private func replaceToken(_ trigger: Trigger, with word: String) {
        let index = text.index(text.startIndex, offsetBy: trigger.start)
        text = String(text[..<index]) + word + " "
    }

    // MARK: lists

    @ViewBuilder private func suggestionPanel(_ trigger: Trigger) -> some View {
        switch trigger.kind {
        case .file:
            if !fileSuggestions.isEmpty {
                SuggestionPanel(title: String(localized: "文件"), rows: fileSuggestions.count, close: { close(trigger) }) {
                    ForEach(fileSuggestions, id: \.self) { path in
                        SuggestionRow(title: path, detail: nil) { replaceToken(trigger, with: "@" + path) }
                    }
                }
            }
        case .command, .skill:
            let matches = matching(trigger.query)
            SuggestionPanel(title: agent == "codex" ? String(localized: "技能") : String(localized: "命令和技能"),
                            rows: max(matches.count, 1), close: { close(trigger) }) {
                if commands != nil {
                    if matches.isEmpty {
                        SuggestionNote(text: trigger.query.isEmpty
                                       ? String(localized: "这个项目里没有找到可用的命令或技能")
                                       : String(localized: "没有匹配的"))
                    }
                    ForEach(matches) { option in
                        SuggestionRow(title: (agent == "codex" ? "$" : "/") + option.name,
                                      detail: option.description, badge: option.kind == "builtin" ? String(localized: "内置") : nil) {
                            replaceToken(trigger, with: (agent == "codex" ? "$" : "/") + option.name)
                        }
                    }
                } else if let commandsError {
                    SuggestionNote(text: commandsError)
                } else {
                    SuggestionNote(text: String(localized: "正在读取命令和技能…"), loading: true)
                }
            }
        }
    }

    private func matching(_ query: String) -> [CommandOption] {
        guard let commands else { return [] }
        guard !query.isEmpty else { return commands }
        let byName = commands.filter { $0.name.localizedCaseInsensitiveContains(query) }
        let prefixed = byName.filter { $0.name.lowercased().hasPrefix(query.lowercased()) }
        let others = byName.filter { !$0.name.lowercased().hasPrefix(query.lowercased()) }
        let byDescription = commands.filter {
            !$0.name.localizedCaseInsensitiveContains(query) && ($0.description ?? "").localizedCaseInsensitiveContains(query)
        }
        return prefixed + others + byDescription
    }

    private func close(_ trigger: Trigger) {
        withAnimation(.snappy) { dismissedAt = trigger.start }
    }

    // MARK: pieces

    private var sendButton: some View {
        Group {
            if !hasContent && isRunning {
                Button(action: onStop) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 14, weight: .bold))
                        .frame(width: 34, height: 34)
                        .background(Circle().fill(Color.primary))
                        .foregroundStyle(Color(.systemBackground))
                }
                .accessibilityLabel("停止")
                .accessibilityIdentifier("stop")
            } else {
                Button(action: onSend) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 16, weight: .bold))
                        .frame(width: 34, height: 34)
                        .background(Circle().fill(hasContent && canSend ? Color.accentColor : Color(.systemGray4)))
                        .foregroundStyle(.white)
                }
                .disabled(!hasContent || !canSend)
                .accessibilityLabel("发送")
                .accessibilityIdentifier("send")
            }
        }
        .buttonStyle(.plain)
    }

    private var attachMenu: some View {
        Menu {
            Button {
                showPhotoPicker = true
            } label: {
                Label("照片和截图", systemImage: "photo.on.rectangle")
            }
            Button {
                pasteImage()
            } label: {
                Label("粘贴图片", systemImage: "doc.on.clipboard")
            }
            .disabled(!UIPasteboard.general.hasImages)
            Button {
                text += (text.isEmpty || text.hasSuffix(" ") ? "@" : " @")
                focused.wrappedValue = true
            } label: {
                Label("引用文件 @", systemImage: "at")
            }
            Button {
                if agent == "codex" {
                    text += (text.isEmpty || text.hasSuffix(" ") ? "$" : " $")
                } else {
                    text = "/" + text
                }
                focused.wrappedValue = true
            } label: {
                Label(agent == "codex" ? "技能 $" : "命令和技能 /", systemImage: "wand.and.stars")
            }
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 17, weight: .semibold))
                .frame(width: 30, height: 30)
                .background(Circle().fill(Color(.tertiarySystemFill)))
        }
        .photosPicker(isPresented: $showPhotoPicker, selection: $pickerItems, maxSelectionCount: 4, matching: .images)
    }

    private var attachmentStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { attachment in
                    ZStack(alignment: .topTrailing) {
                        if let image = UIImage(data: attachment.data) {
                            Image(uiImage: image)
                                .resizable()
                                .scaledToFill()
                                .frame(width: 56, height: 56)
                                .clipShape(RoundedRectangle(cornerRadius: 10))
                        }
                        Button {
                            attachments.removeAll { $0.id == attachment.id }
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .symbolRenderingMode(.palette)
                                .foregroundStyle(.white, .black.opacity(0.6))
                        }
                        .offset(x: 6, y: -6)
                    }
                }
            }
            .padding(.top, 6)
        }
    }

    // MARK: loading

    private func loadFiles() async {
        guard let query = fileQuery, let cwd, let conn else {
            fileSuggestions = []
            return
        }
        try? await Task.sleep(for: .milliseconds(150))
        guard !Task.isCancelled else { return }
        let list: FileList? = try? await conn.call("files.search", [
            "cwd": .string(cwd), "query": .string(query), "limit": 30,
        ])
        if !Task.isCancelled { fileSuggestions = list?.files ?? [] }
    }

    private func loadCommands() async {
        guard commandKey != nil, let cwd, let conn else { return }
        commandsError = nil
        commands = conn.cachedCommands(agent: agent, cwd: cwd)
        do {
            let list = try await conn.commands(agent: agent, cwd: cwd)
            if !Task.isCancelled { commands = list }
        } catch {
            if !Task.isCancelled, commands == nil {
                commandsError = String(localized: "没能读取命令：\(error.localizedDescription)")
            }
        }
    }

    private func loadPicked(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        Task {
            for item in items {
                if let data = try? await item.loadTransferable(type: Data.self),
                    let attachment = ImageAttachment.make(from: data)
                {
                    attachments.append(attachment)
                }
            }
            pickerItems = []
        }
    }

    private func pasteImage() {
        guard let image = UIPasteboard.general.image, let data = image.pngData(),
            let attachment = ImageAttachment.make(from: data)
        else { return }
        attachments.append(attachment)
    }
}

/// The agent's suggested next message: tap to send, pencil to edit first.
struct ReplySuggestionRow: View {
    let text: String
    let enabled: Bool
    let send: () -> Void
    let edit: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            Button(action: send) {
                HStack(spacing: 6) {
                    Image(systemName: "sparkles").font(.caption.weight(.semibold)).foregroundStyle(Color.accentColor)
                    Text(text).lineLimit(2).multilineTextAlignment(.leading)
                    Spacer(minLength: 0)
                    Image(systemName: "arrow.up.circle.fill").foregroundStyle(Color.accentColor)
                }
                .font(.subheadline)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Capsule().strokeBorder(Color.accentColor.opacity(0.35)))
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .disabled(!enabled)
            .accessibilityIdentifier("replySuggestion")
            Button(action: edit) {
                Image(systemName: "pencil")
                    .font(.footnote.weight(.semibold))
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(Color(.tertiarySystemFill)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("改一改再发")
        }
    }
}

/// A floating list above the composer with a title and a close button.
struct SuggestionPanel<Content: View>: View {
    let title: String
    let rows: Int
    let close: () -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button(action: close) {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .bold))
                        .frame(width: 26, height: 26)
                        .background(Circle().fill(Color(.tertiarySystemFill)))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭列表")
                .accessibilityIdentifier("closeSuggestions")
            }
            .padding(.leading, 14)
            .padding(.trailing, 8)
            .padding(.vertical, 6)
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) { content() }
            }
            .frame(height: min(240, CGFloat(rows) * 52))
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemBackground)).shadow(color: .black.opacity(0.12), radius: 10))
        .transition(.opacity)
    }
}

struct SuggestionRow: View {
    let title: String
    let detail: String?
    var badge: String? = nil
    let pick: () -> Void

    var body: some View {
        Button(action: pick) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(title).font(.system(.callout, design: .monospaced)).lineLimit(1)
                    if let badge {
                        Text(badge).font(.caption2).foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().strokeBorder(Color(.separator)))
                    }
                }
                if let detail, !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        Divider().padding(.leading, 14)
    }
}

struct SuggestionNote: View {
    let text: String
    var loading = false

    var body: some View {
        HStack(spacing: 8) {
            if loading { ProgressView().controlSize(.small) }
            Text(text).font(.footnote).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
    }
}

struct FullScreenEditor: View {
    @Binding var text: String
    let placeholder: String
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            TextEditor(text: $text)
                .focused($focused)
                .padding(.horizontal, 12)
                .overlay(alignment: .topLeading) {
                    if text.isEmpty {
                        Text(placeholder).foregroundStyle(.tertiary).padding(.horizontal, 17).padding(.top, 8)
                            .allowsHitTesting(false)
                    }
                }
                .navigationTitle("编辑消息")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("完成") { dismiss() }
                    }
                }
                .onAppear { focused = true }
        }
    }
}
