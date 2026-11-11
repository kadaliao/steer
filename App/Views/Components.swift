import SwiftUI

extension Color {
    static let claude = Color(red: 0.85, green: 0.47, blue: 0.34)
    static let codex = Color(red: 0.13, green: 0.13, blue: 0.15)
    static let needsYou = Color.orange
}

/// Small round mark that tells Claude and Codex sessions apart.
struct AgentBadge: View {
    let agent: String
    var size: CGFloat = 30

    var body: some View {
        ZStack {
            Circle().fill(agent == "codex" ? Color.primary.opacity(0.88) : Color.claude)
            Image(systemName: agent == "codex" ? "circle.hexagongrid" : "asterisk")
                .font(.system(size: size * 0.48, weight: .bold))
                .foregroundStyle(agent == "codex" ? Color(.systemBackground) : .white)
        }
        .frame(width: size, height: size)
        .accessibilityLabel(Agents.name(agent))
    }
}

/// A rounded pill used for project, agent, mode and model choices.
struct ChipLabel: View {
    let text: String
    var systemImage: String?
    var prominent = false

    var body: some View {
        HStack(spacing: 4) {
            if let systemImage {
                Image(systemName: systemImage).font(.caption.weight(.semibold))
            }
            Text(text).lineLimit(1)
            Image(systemName: "chevron.down").font(.system(size: 9, weight: .bold)).opacity(0.6)
        }
        .font(.footnote.weight(.medium))
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Capsule().fill(prominent ? Color.accentColor.opacity(0.15) : Color(.tertiarySystemFill)))
        .foregroundStyle(prominent ? Color.accentColor : Color.primary)
    }
}

/// Status text for a session row: waiting, running clock, or time ago.
struct SessionStatus: View {
    let summary: SessionSummary
    let unread: Bool

    var body: some View {
        if summary.isWaiting {
            Label(needsText, systemImage: "hand.raised.fill")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Color.needsYou)
                .labelStyle(.titleAndIcon)
        } else if summary.isRunning {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                HStack(spacing: 4) {
                    ProgressView().controlSize(.mini)
                    if let since = summary.runningSince {
                        Text(Durations.clock(since: since, now: context.date)).monospacedDigit()
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        } else {
            HStack(spacing: 5) {
                if let date = summary.updatedDate {
                    Text(date, format: .relative(presentation: .named))
                }
                if unread {
                    Circle().fill(Color.accentColor).frame(width: 8, height: 8)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private var needsText: String {
        switch summary.needs {
        case "question": String(localized: "等你回答")
        case "plan": String(localized: "等你批准计划")
        default: String(localized: "等你批准")
        }
    }
}

/// A host's link trouble or progress, shown above the session list. Tap a
/// failure to retry.
struct ConnectionBanner: View {
    let conn: HostConnection
    var showName = false

    var body: some View {
        switch conn.status {
        case .connected, .idle:
            EmptyView()
        case .connecting(let text):
            banner(text, systemImage: nil, tint: .secondary)
        case .failed(let text):
            Button {
                Task { await conn.connect() }
            } label: {
                banner(text, systemImage: "exclamationmark.triangle.fill", tint: .orange)
            }
            .buttonStyle(.plain)
        }
    }

    private func banner(_ text: String, systemImage: String?, tint: Color) -> some View {
        HStack(spacing: 8) {
            if let systemImage {
                Image(systemName: systemImage).foregroundStyle(tint)
            } else {
                ProgressView().controlSize(.small)
            }
            Group {
                if showName {
                    Text(conn.name).fontWeight(.semibold) + Text("：") + Text(text)
                } else {
                    Text(text)
                }
            }
            .font(.footnote).foregroundStyle(.primary).lineLimit(3)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
    }
}

/// Green when connected, orange when failing, grey otherwise.
struct StatusDot: View {
    let status: LinkStatus

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 7, height: 7)
    }

    private var color: Color {
        switch status {
        case .connected: .green
        case .failed: .orange
        case .connecting: .yellow
        case .idle: Color(.systemGray3)
        }
    }
}

enum HostStatusText {
    static func text(_ status: LinkStatus) -> String {
        switch status {
        case .connected: String(localized: "已连接")
        case .connecting: String(localized: "正在连接")
        case .failed(let text): text
        case .idle: String(localized: "未连接")
        }
    }
}
