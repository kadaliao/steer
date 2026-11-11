import SwiftUI

enum Route: Hashable {
    case chat(String)
    case newChat
    case settings
}

struct HomeView: View {
    @Environment(AppModel.self) private var app
    @State private var path: [Route] = []

    var body: some View {
        NavigationStack(path: $path) {
            SessionListView(open: { path.append($0) })
                .navigationDestination(for: Route.self) { route in
                    switch route {
                    case .chat(let sid):
                        ChatScreen(sid: sid)
                    case .newChat:
                        NewChatView { sid in
                            // Replace the draft screen with the new conversation.
                            if !path.isEmpty { path.removeLast() }
                            path.append(.chat(sid))
                        }
                    case .settings:
                        SettingsView()
                    }
                }
        }
    }
}

struct SessionListView: View {
    @Environment(AppModel.self) private var app
    let open: (Route) -> Void
    @State private var filter: String?

    private var visible: [SessionSummary] {
        app.sessions.filter { filter == nil || $0.cwd == filter }
    }

    private var needsYou: [SessionSummary] { visible.filter(\.isWaiting) }
    private var rest: [SessionSummary] { visible.filter { !$0.isWaiting } }

    /// Projects with the most recent activity, for the filter chips.
    private var topProjects: [String] {
        var seen: [String] = []
        for summary in app.sessions where !seen.contains(summary.cwd) {
            seen.append(summary.cwd)
            if seen.count == 8 { break }
        }
        return seen
    }

    var body: some View {
        List {
            ConnectionBanner()
                .listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
            if topProjects.count > 1 {
                filterChips
                    .listRowSeparator(.hidden)
                    .listRowInsets(EdgeInsets(top: 0, leading: 0, bottom: 4, trailing: 0))
            }
            if !needsYou.isEmpty {
                Section {
                    ForEach(needsYou) { row($0) }
                } header: {
                    Text("需要你").foregroundStyle(Color.needsYou)
                }
            }
            Section {
                ForEach(rest) { row($0) }
            } header: {
                if !needsYou.isEmpty { Text("其他") }
            }
            if app.sessions.isEmpty, app.isConnected {
                emptyState.listRowSeparator(.hidden)
            }
        }
        .listStyle(.plain)
        .refreshable { await app.refreshSessions() }
        .navigationTitle(app.activeHost?.displayName ?? "Steer")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button { open(.settings) } label: { Image(systemName: "gearshape") }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { open(.newChat) } label: {
                    Image(systemName: "square.and.pencil").font(.body.weight(.semibold))
                }
                .disabled(!app.isConnected)
            }
        }
        .overlay(alignment: .bottomTrailing) {
            Button { open(.newChat) } label: {
                Image(systemName: "plus")
                    .font(.system(size: 24, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 60, height: 60)
                    .background(Circle().fill(Color.accentColor).shadow(color: .black.opacity(0.2), radius: 8, y: 3))
            }
            .padding(22)
            .disabled(!app.isConnected)
            .accessibilityLabel("新会话")
        }
    }

    private var filterChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                chip(String(localized: "全部"), selected: filter == nil) { filter = nil }
                ForEach(topProjects, id: \.self) { cwd in
                    chip((cwd as NSString).lastPathComponent, selected: filter == cwd) {
                        filter = filter == cwd ? nil : cwd
                    }
                }
            }
            .padding(.horizontal, 16)
        }
    }

    private func chip(_ title: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.footnote.weight(.medium))
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(Capsule().fill(selected ? Color.primary : Color(.tertiarySystemFill)))
                .foregroundStyle(selected ? Color(.systemBackground) : Color.primary)
        }
        .buttonStyle(.plain)
    }

    private func row(_ summary: SessionSummary) -> some View {
        Button {
            open(.chat(summary.sid))
        } label: {
            SessionRow(summary: summary, unread: app.isUnread(summary))
        }
        .swipeActions(edge: .trailing) {
            Button(role: .destructive) {
                Task { await app.archive(summary) }
            } label: { Label("归档", systemImage: "archivebox") }
            if summary.isRunning {
                Button {
                    Task { await app.chat(for: summary).interrupt() }
                } label: { Label("停止", systemImage: "stop.circle") }
                .tint(.orange)
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "bubble.left.and.text.bubble.right").font(.largeTitle).foregroundStyle(.secondary)
            Text("还没有会话").font(.headline)
            Text("点右下角的 + 开始，或者在电脑上用过 Claude Code / Codex 后，这里会列出来接着聊。")
                .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 60)
    }
}

struct SessionRow: View {
    let summary: SessionSummary
    let unread: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            AgentBadge(agent: summary.agent, size: 32)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(summary.title.isEmpty ? String(localized: "未命名") : summary.title)
                        .font(.body.weight(unread || summary.isWaiting ? .semibold : .regular))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    SessionStatus(summary: summary, unread: unread)
                }
                HStack(spacing: 6) {
                    Text(summary.projectName).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    if summary.isDesktop {
                        Text("电脑上").font(.caption2).padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().strokeBorder(Color(.separator)))
                            .foregroundStyle(.secondary)
                    }
                }
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }

    private var subtitle: String? {
        if summary.isRunning, let activity = summary.activity, let target = activity.target, !target.isEmpty {
            return ToolText.activity(activity.category, target)
        }
        if let error = summary.error, !error.isEmpty { return error }
        return summary.lastText.map(PlainPreview.make)
    }
}
