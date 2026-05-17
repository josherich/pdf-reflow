import SwiftUI
import WebKit

struct SettingsView: View {
    @ObservedObject var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    @State private var cacheFileCount: Int = 0
    @State private var cacheByteSize: Int64 = 0
    @State private var scanning = false
    @State private var clearing = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Cache") {
                    HStack {
                        Text("Files")
                        Spacer()
                        Text(scanning ? "—" : "\(cacheFileCount)")
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                    HStack {
                        Text("Size")
                        Spacer()
                        Text(scanning
                             ? "—"
                             : ByteCountFormatter.string(fromByteCount: cacheByteSize, countStyle: .file))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                    Button(role: .destructive) {
                        Task { await clearCache() }
                    } label: {
                        HStack {
                            if clearing { ProgressView().padding(.trailing, 6) }
                            Text("Clear cache")
                        }
                    }
                    .disabled(clearing || (cacheFileCount == 0 && cacheByteSize == 0))
                }

                Section("Reflow") {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("Font size")
                            Spacer()
                            Text("\(Int(settings.fontSize)) pt")
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                        Slider(
                            value: $settings.fontSize,
                            in: AppSettings.fontRange,
                            step: 1
                        )
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("Image quality")
                            Spacer()
                            Text("\(Int(settings.imagePPI)) ppi")
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                        Slider(
                            value: $settings.imagePPI,
                            in: AppSettings.ppiRange,
                            step: 1
                        )
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .task { await refreshCacheStats() }
        }
    }

    private func refreshCacheStats() async {
        scanning = true
        let stats = await Task.detached(priority: .utility) {
            CacheInspector.scan()
        }.value
        cacheFileCount = stats.fileCount
        cacheByteSize = stats.totalBytes
        scanning = false
    }

    private func clearCache() async {
        clearing = true
        await Task.detached(priority: .utility) {
            CacheInspector.clear()
        }.value
        await clearWebData()
        clearing = false
        await refreshCacheStats()
    }

    private func clearWebData() async {
        URLCache.shared.removeAllCachedResponses()
        let types = WKWebsiteDataStore.allWebsiteDataTypes()
        await WKWebsiteDataStore.default().removeData(
            ofTypes: types,
            modifiedSince: .distantPast
        )
    }
}

enum CacheInspector {
    struct Stats { let fileCount: Int; let totalBytes: Int64 }

    static func cacheDirectories() -> [URL] {
        var dirs: [URL] = []
        let fm = FileManager.default
        if let caches = try? fm.url(for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: false) {
            dirs.append(caches)
        }
        let tmp = fm.temporaryDirectory
        dirs.append(tmp)
        return dirs
    }

    static func scan() -> Stats {
        let fm = FileManager.default
        var count = 0
        var bytes: Int64 = 0
        for root in cacheDirectories() {
            guard let enumerator = fm.enumerator(
                at: root,
                includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey],
                options: [.skipsHiddenFiles]
            ) else { continue }
            for case let url as URL in enumerator {
                guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
                      values.isRegularFile == true else { continue }
                count += 1
                bytes += Int64(values.fileSize ?? 0)
            }
        }
        return Stats(fileCount: count, totalBytes: bytes)
    }

    static func clear() {
        let fm = FileManager.default
        for root in cacheDirectories() {
            guard let contents = try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { continue }
            for item in contents {
                try? fm.removeItem(at: item)
            }
        }
    }
}
