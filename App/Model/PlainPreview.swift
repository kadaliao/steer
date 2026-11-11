import Foundation

/// Turns the start of an agent's Markdown reply into one line of plain text
/// for list previews.
enum PlainPreview {
    static func make(_ text: String) -> String {
        var out = text
        for marker in ["```", "**", "__", "`"] {
            out = out.replacingOccurrences(of: marker, with: marker == "```" ? " " : "")
        }
        out = out.replacingOccurrences(of: #"(^|\s)#{1,6}\s"#, with: "$1", options: .regularExpression)
        out = out.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        return out.trimmingCharacters(in: .whitespaces)
    }
}
