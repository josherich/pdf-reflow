import Foundation
import CryptoKit

/// On-disk cache of reflowed PDFs.
///
/// Keys are derived from the SHA-256 of the original file bytes plus the
/// reflow configuration (font size, PPI), so renaming or moving a file
/// still hits the same cache entry, and changing settings produces a new
/// one.
final class ReflowCache {
    static let shared = ReflowCache()

    let directory: URL

    private init() {
        let fm = FileManager.default
        let base = (try? fm.url(
            for: .cachesDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )) ?? fm.temporaryDirectory
        directory = base.appendingPathComponent("ReflowCache", isDirectory: true)
        try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func signature(for pdfData: Data) -> String {
        let digest = SHA256.hash(data: pdfData)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    func key(signature: String, fontSize: Double, ppi: Double) -> String {
        "\(signature)-f\(Int(fontSize.rounded()))-p\(Int(ppi.rounded()))"
    }

    func url(for key: String) -> URL {
        directory.appendingPathComponent("\(key).pdf")
    }

    func read(key: String) -> Data? {
        let target = url(for: key)
        guard FileManager.default.fileExists(atPath: target.path) else { return nil }
        let data = try? Data(contentsOf: target)
        if data != nil {
            // Touch mtime so LRU-style cleanups (future) work.
            try? FileManager.default.setAttributes(
                [.modificationDate: Date()],
                ofItemAtPath: target.path
            )
        }
        return data
    }

    func write(key: String, data: Data) {
        let target = url(for: key)
        try? data.write(to: target, options: .atomic)
    }

    struct Stats { let fileCount: Int; let totalBytes: Int64 }

    func stats() -> Stats {
        let fm = FileManager.default
        guard let items = try? fm.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [.fileSizeKey, .isRegularFileKey]
        ) else {
            return Stats(fileCount: 0, totalBytes: 0)
        }
        var count = 0
        var bytes: Int64 = 0
        for item in items {
            guard let v = try? item.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
                  v.isRegularFile == true else { continue }
            count += 1
            bytes += Int64(v.fileSize ?? 0)
        }
        return Stats(fileCount: count, totalBytes: bytes)
    }

    /// Remove every cached variant (any font size / PPI) for a given PDF signature.
    func removeAll(signature: String) {
        let fm = FileManager.default
        guard let items = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else { return }
        let prefix = "\(signature)-"
        for item in items where item.lastPathComponent.hasPrefix(prefix) {
            try? fm.removeItem(at: item)
        }
    }

    func clear() {
        let fm = FileManager.default
        guard let items = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else { return }
        for item in items {
            try? fm.removeItem(at: item)
        }
    }
}
