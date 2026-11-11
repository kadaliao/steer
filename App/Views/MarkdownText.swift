import SwiftUI
import UIKit

/// Renders the Markdown agents write: paragraphs with inline styling,
/// headings, lists, quotes and fenced code blocks.
struct MarkdownText: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(MarkdownBlock.parse(text).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .paragraph(let text):
            Text(Self.inline(text)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        case .heading(let level, let text):
            Text(Self.inline(text))
                .font(level <= 1 ? .title3.bold() : level == 2 ? .headline : .subheadline.bold())
                .padding(.top, 2)
        case .listItem(let marker, let indent, let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(marker).foregroundStyle(.secondary).monospacedDigit()
                Text(Self.inline(text)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, CGFloat(indent) * 14)
        case .quote(let text):
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 1).fill(Color.secondary.opacity(0.4)).frame(width: 3)
                Text(Self.inline(text)).foregroundStyle(.secondary)
            }
        case .code(let language, let code):
            CodeBlock(language: language, code: code)
        case .rule:
            Divider()
        }
    }

    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

struct CodeBlock: View {
    let language: String
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language).font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button {
                    UIPasteboard.general.string = code
                    copied = true
                } label: {
                    Image(systemName: copied ? "checkmark" : "doc.on.doc").font(.caption)
                }
                .buttonStyle(.borderless)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(.footnote, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(.horizontal, 10)
                    .padding(.bottom, 10)
            }
        }
        .background(RoundedRectangle(cornerRadius: 10).fill(Color(.secondarySystemBackground)))
    }
}

enum MarkdownBlock: Equatable {
    case paragraph(String)
    case heading(Int, String)
    case listItem(String, Int, String)
    case quote(String)
    case code(String, String)
    case rule

    static func parse(_ text: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var code: [String]?
        var language = ""

        func flushParagraph() {
            let joined = paragraph.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty { blocks.append(.paragraph(joined)) }
            paragraph = []
        }

        for rawLine in text.components(separatedBy: "\n") {
            let line = rawLine
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if var lines = code {
                if trimmed.hasPrefix("```") {
                    blocks.append(.code(language, lines.joined(separator: "\n")))
                    code = nil
                } else {
                    lines.append(line)
                    code = lines
                }
                continue
            }
            if trimmed.hasPrefix("```") {
                flushParagraph()
                language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                code = []
                continue
            }
            if trimmed.isEmpty {
                flushParagraph()
                continue
            }
            if trimmed == "---" || trimmed == "***" || trimmed == "___" {
                flushParagraph()
                blocks.append(.rule)
                continue
            }
            if trimmed.hasPrefix("#") {
                let level = trimmed.prefix { $0 == "#" }.count
                if level <= 6, trimmed.dropFirst(level).first == " " {
                    flushParagraph()
                    blocks.append(.heading(level, String(trimmed.dropFirst(level + 1))))
                    continue
                }
            }
            if trimmed.hasPrefix("> ") {
                flushParagraph()
                blocks.append(.quote(String(trimmed.dropFirst(2))))
                continue
            }
            let indent = (line.prefix { $0 == " " }.count) / 2
            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
                flushParagraph()
                var body = String(trimmed.dropFirst(2))
                var marker = "•"
                if body.hasPrefix("[ ] ") { marker = "☐"; body = String(body.dropFirst(4)) }
                if body.hasPrefix("[x] ") || body.hasPrefix("[X] ") { marker = "☑"; body = String(body.dropFirst(4)) }
                blocks.append(.listItem(marker, indent, body))
                continue
            }
            if let dot = trimmed.firstIndex(of: "."), trimmed[..<dot].allSatisfy(\.isNumber), !trimmed[..<dot].isEmpty,
                trimmed[trimmed.index(after: dot)...].hasPrefix(" ")
            {
                flushParagraph()
                blocks.append(.listItem(String(trimmed[...dot]), indent, String(trimmed[trimmed.index(dot, offsetBy: 2)...])))
                continue
            }
            paragraph.append(line)
        }
        if let lines = code {
            blocks.append(.code(language, lines.joined(separator: "\n")))
        }
        flushParagraph()
        return blocks
    }
}
