import SwiftUI

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
                Section {
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
                } header: {
                    Text("Reflow cache")
                } footer: {
                    Text("Reflowed PDFs are stored on disk and reused when you open the same file (matched by content, not name).")
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
            ReflowCache.shared.stats()
        }.value
        cacheFileCount = stats.fileCount
        cacheByteSize = stats.totalBytes
        scanning = false
    }

    private func clearCache() async {
        clearing = true
        await Task.detached(priority: .utility) {
            ReflowCache.shared.clear()
        }.value
        clearing = false
        await refreshCacheStats()
    }
}
