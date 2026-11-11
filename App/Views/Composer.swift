import PhotosUI
import SwiftUI
import UIKit

/// The message box. Uses the system keyboard (and so any input method and
/// dictation), grows with the text, and can expand to a full-screen editor.
struct Composer<Chips: View>: View {
    @Environment(AppModel.self) private var app
    @Binding var text: String
    @Binding var attachments: [ImageAttachment]
    var focused: FocusState<Bool>.Binding
    let agent: String
    let cwd: String?
    let placeholder: String
    let isRunning: Bool
    let showQuickPhrases: Bool
    let canSend: Bool
    let onSend: () -> Void
    let onStop: () -> Void
    let onQuickPhrase: (String) -> Void
    @ViewBuilder let chips: () -> Chips

    @State private var pickerItems: [PhotosPickerItem] = []
    @State private var showEditor = false
    @State private var fileSuggestions: [String] = []
    @State private var commands: [CommandOption] = []
    @State private var phrases = Prefs.quickPhrases

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var hasContent: Bool { !trimmed.isEmpty || !attachments.isEmpty }

    /// The `@word` being typed at the end of the text, if any.
    private var mentionQuery: String? {
        guard let last = text.split(separator: " ", omittingEmptySubsequences: false).last,
            last.hasPrefix("@"), !last.contains("\n")
        else { return nil }
        return String(last.dropFirst())
    }

    private var commandQuery: String? {
        guard text.hasPrefix("/"), !text.contains(" "), !text.contains("\n") else { return nil }
        return String(text.dropFirst())
    }

    var body: some View {
        VStack(spacing: 8) {
            suggestions
            if showQuickPhrases && text.isEmpty && attachments.isEmpty {
                quickPhraseBar
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
                    sendButton
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)
            .padding(.bottom, 8)
            .background(RoundedRectangle(cornerRadius: 24).fill(Color(.secondarySystemBackground)))
        }
        .onChange(of: pickerItems) { _, items in loadPicked(items) }
        .task(id: mentionQuery) { await loadFiles() }
        .task(id: commandQuery == nil) { await loadCommands() }
        .sheet(isPresented: $showEditor) {
            FullScreenEditor(text: $text, placeholder: placeholder)
        }
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
                text = "/"
                focused.wrappedValue = true
            } label: {
                Label("命令 /", systemImage: "slash.circle")
            }
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 17, weight: .semibold))
                .frame(width: 30, height: 30)
                .background(Circle().fill(Color(.tertiarySystemFill)))
        }
        .photosPicker(isPresented: $showPhotoPicker, selection: $pickerItems, maxSelectionCount: 4, matching: .images)
    }

    @State private var showPhotoPicker = false

    private var quickPhraseBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(phrases, id: \.self) { phrase in
                    Button {
                        onQuickPhrase(phrase)
                    } label: {
                        Text(phrase)
                            .font(.footnote.weight(.medium))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(Capsule().strokeBorder(Color(.separator)))
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSend)
                }
            }
            .padding(.horizontal, 2)
        }
        .onAppear { phrases = Prefs.quickPhrases }
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

    @ViewBuilder private var suggestions: some View {
        if mentionQuery != nil, !fileSuggestions.isEmpty {
            SuggestionList(entries: fileSuggestions.map { ($0, nil) }) { path in
                var parts = text.split(separator: " ", omittingEmptySubsequences: false).map(String.init)
                parts.removeLast()
                parts.append("@" + path)
                text = parts.joined(separator: " ") + " "
            }
        } else if let query = commandQuery {
            let matches = commands.filter { query.isEmpty || $0.name.localizedCaseInsensitiveContains(query) }.prefix(30)
            if !matches.isEmpty {
                SuggestionList(entries: matches.map { ("/" + $0.name, $0.description) }) { name in
                    text = name + " "
                }
            }
        }
    }

    // MARK: loading

    private func loadFiles() async {
        guard let query = mentionQuery, let cwd else {
            fileSuggestions = []
            return
        }
        try? await Task.sleep(for: .milliseconds(150))
        guard !Task.isCancelled else { return }
        let list: FileList? = try? await app.call("files.search", [
            "cwd": .string(cwd), "query": .string(query), "limit": 30,
        ])
        if !Task.isCancelled { fileSuggestions = list?.files ?? [] }
    }

    private func loadCommands() async {
        guard commandQuery != nil, commands.isEmpty else { return }
        commands = await app.info(for: agent)?.commands ?? []
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

struct SuggestionList: View {
    let entries: [(String, String?)]
    let pick: (String) -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(entries, id: \.0) { entry in
                    Button {
                        pick(entry.0)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(entry.0).font(.system(.callout, design: .monospaced)).lineLimit(1)
                            if let detail = entry.1, !detail.isEmpty {
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
        }
        .frame(maxHeight: 220)
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemBackground)).shadow(color: .black.opacity(0.12), radius: 10))
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
