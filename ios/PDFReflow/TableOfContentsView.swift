import SwiftUI
import PDFKit

struct TableOfContentsView: View {
    let document: PDFDocument
    let currentPageIndex: Int?
    let onSelect: (Int) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var query: String = ""

    private var entries: [TOCEntry] { TOCEntry.build(from: document) }

    private var flattened: [TOCEntry] {
        var out: [TOCEntry] = []
        TOCEntry.flatten(entries, into: &out)
        return out
    }

    private var filtered: [TOCEntry] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return flattened }
        let needle = trimmed.lowercased()
        return flattened.filter { $0.label.lowercased().contains(needle) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if entries.isEmpty {
                    EmptyTOCView()
                } else if filtered.isEmpty {
                    NoMatchesView(query: query)
                } else {
                    List(filtered) { entry in
                        TOCRow(
                            entry: entry,
                            isCurrent: entry.pageIndex == currentPageIndex
                        )
                        .contentShape(Rectangle())
                        .onTapGesture { select(entry) }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Contents")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always))
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func select(_ entry: TOCEntry) {
        guard let idx = entry.pageIndex else { return }
        onSelect(idx)
        dismiss()
    }
}

private struct TOCEntry: Identifiable {
    let id = UUID()
    let label: String
    let pageIndex: Int?
    let pageLabel: String?
    let depth: Int
    let children: [TOCEntry]

    static func build(from doc: PDFDocument) -> [TOCEntry] {
        guard let root = doc.outlineRoot else { return [] }
        return children(of: root, depth: 0, doc: doc)
    }

    private static func children(
        of node: PDFOutline, depth: Int, doc: PDFDocument
    ) -> [TOCEntry] {
        var out: [TOCEntry] = []
        for i in 0..<node.numberOfChildren {
            guard let child = node.child(at: i) else { continue }
            var pageIndex: Int? = nil
            var pageLabel: String? = nil
            if let dest = child.destination, let page = dest.page {
                let idx = doc.index(for: page)
                if idx >= 0 {
                    pageIndex = idx
                    pageLabel = page.label
                }
            }
            let label = (child.label?.trimmingCharacters(in: .whitespacesAndNewlines)).flatMap {
                $0.isEmpty ? nil : $0
            } ?? "Untitled"
            out.append(TOCEntry(
                label: label,
                pageIndex: pageIndex,
                pageLabel: pageLabel,
                depth: depth,
                children: children(of: child, depth: depth + 1, doc: doc)
            ))
        }
        return out
    }

    static func flatten(_ entries: [TOCEntry], into out: inout [TOCEntry]) {
        for entry in entries {
            out.append(entry)
            flatten(entry.children, into: &out)
        }
    }
}

private struct TOCRow: View {
    let entry: TOCEntry
    let isCurrent: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(entry.label)
                .font(entry.depth == 0 ? .body.weight(.semibold) : .body)
                .foregroundStyle(entry.pageIndex == nil ? .secondary : .primary)
                .lineLimit(3)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let pageLabel = entry.pageLabel ?? entry.pageIndex.map({ String($0 + 1) }) {
                Text(pageLabel)
                    .font(.footnote.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.leading, CGFloat(entry.depth) * 16)
        .padding(.vertical, 4)
        .background(
            isCurrent ? Color.accentColor.opacity(0.12) : Color.clear
        )
    }
}

private struct EmptyTOCView: View {
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "list.bullet.indent")
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(.secondary)
            Text("No table of contents")
                .font(.headline)
            Text("This PDF doesn't include an outline.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct NoMatchesView: View {
    let query: String

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 36, weight: .light))
                .foregroundStyle(.secondary)
            Text("No entries match “\(query)”")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
