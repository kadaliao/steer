import SwiftUI

/// A unified diff with added and removed lines tinted. Long lines wrap so
/// nothing needs horizontal scrolling on a phone.
struct DiffView: View {
    let diff: String
    var maxLines = 600

    var body: some View {
        let lines = Array(diff.components(separatedBy: "\n").prefix(maxLines))
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                DiffLine(line: line)
            }
            if diff.components(separatedBy: "\n").count > maxLines {
                Text("…").font(.caption).foregroundStyle(.secondary).padding(6)
            }
        }
        .textSelection(.enabled)
    }
}

private struct DiffLine: View {
    let line: String

    var body: some View {
        Text(line.isEmpty ? " " : line)
            .font(.system(size: 12, design: .monospaced))
            .foregroundStyle(foreground)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 8)
            .padding(.vertical, 1)
            .background(background)
    }

    private var isFileHeader: Bool { line.hasPrefix("+++") || line.hasPrefix("---") }

    private var foreground: Color {
        if isFileHeader { return .primary }
        if line.hasPrefix("@@") { return .secondary }
        return .primary
    }

    private var background: Color {
        if isFileHeader { return Color(.tertiarySystemFill) }
        if line.hasPrefix("+") { return Color.green.opacity(0.16) }
        if line.hasPrefix("-") { return Color.red.opacity(0.14) }
        if line.hasPrefix("@@") { return Color.blue.opacity(0.07) }
        return .clear
    }
}

struct DiffCounts: View {
    let add: Int?
    let del: Int?

    var body: some View {
        HStack(spacing: 4) {
            if let add { Text("+\(add)").foregroundStyle(.green) }
            if let del { Text("−\(del)").foregroundStyle(.red) }
        }
        .font(.caption.monospacedDigit().weight(.medium))
    }
}
