import Foundation

struct RecentPDF: Identifiable, Codable, Equatable {
    var id: UUID
    var name: String
    var bookmark: Data
    var lastOpened: Date
    var size: Int64
    /// SHA-256 of the PDF bytes, used to locate cached reflowed variants.
    /// Optional for backward compatibility with entries persisted before this
    /// field existed.
    var signature: String?

    init(
        id: UUID = UUID(),
        name: String,
        bookmark: Data,
        lastOpened: Date,
        size: Int64,
        signature: String? = nil
    ) {
        self.id = id
        self.name = name
        self.bookmark = bookmark
        self.lastOpened = lastOpened
        self.size = size
        self.signature = signature
    }
}

enum RecentsSort: String, CaseIterable, Identifiable {
    case name
    case dateNewest
    case dateOldest
    case size

    var id: String { rawValue }

    var label: String {
        switch self {
        case .name: return "Name"
        case .dateNewest: return "Date (newest)"
        case .dateOldest: return "Date (oldest)"
        case .size: return "Size"
        }
    }
}

@MainActor
final class RecentPDFsStore: ObservableObject {
    private static let storageKey = "RecentPDFs.v1"
    private static let sortKey = "RecentPDFs.sort"
    private static let maxItems = 500

    @Published private(set) var items: [RecentPDF] = []
    @Published var sort: RecentsSort {
        didSet {
            UserDefaults.standard.set(sort.rawValue, forKey: Self.sortKey)
        }
    }

    init() {
        if let raw = UserDefaults.standard.string(forKey: Self.sortKey),
           let s = RecentsSort(rawValue: raw) {
            self.sort = s
        } else {
            self.sort = .dateNewest
        }
        load()
    }

    var sorted: [RecentPDF] {
        switch sort {
        case .name:
            return items.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        case .dateNewest:
            return items.sorted { $0.lastOpened > $1.lastOpened }
        case .dateOldest:
            return items.sorted { $0.lastOpened < $1.lastOpened }
        case .size:
            return items.sorted { $0.size > $1.size }
        }
    }

    func record(url: URL, size: Int64, signature: String? = nil) {
        let name = url.deletingPathExtension().lastPathComponent
        let bookmark: Data
        do {
            bookmark = try url.bookmarkData(
                options: .minimalBookmark,
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
        } catch {
            return
        }

        items.removeAll { existing in
            (try? matchesBookmark(existing.bookmark, url: url)) == true
                || existing.name == name
        }

        items.insert(
            RecentPDF(
                name: name,
                bookmark: bookmark,
                lastOpened: Date(),
                size: size,
                signature: signature
            ),
            at: 0
        )
        if items.count > Self.maxItems {
            items.removeLast(items.count - Self.maxItems)
        }
        persist()
    }

    func remove(_ item: RecentPDF) {
        items.removeAll { $0.id == item.id }
        persist()
    }

    func resolve(_ item: RecentPDF) -> (url: URL, isStale: Bool)? {
        var isStale = false
        do {
            let url = try URL(
                resolvingBookmarkData: item.bookmark,
                options: [],
                relativeTo: nil,
                bookmarkDataIsStale: &isStale
            )
            return (url, isStale)
        } catch {
            return nil
        }
    }

    private func matchesBookmark(_ data: Data, url: URL) throws -> Bool {
        var stale = false
        let resolved = try URL(
            resolvingBookmarkData: data,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &stale
        )
        return resolved.standardizedFileURL == url.standardizedFileURL
    }

    private func load() {
        guard let data = UserDefaults.standard.data(forKey: Self.storageKey),
              let decoded = try? JSONDecoder().decode([RecentPDF].self, from: data) else {
            return
        }
        items = decoded
    }

    private func persist() {
        guard let data = try? JSONEncoder().encode(items) else { return }
        UserDefaults.standard.set(data, forKey: Self.storageKey)
    }
}
