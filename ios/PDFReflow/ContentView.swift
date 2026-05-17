import SwiftUI
import PDFKit
import UniformTypeIdentifiers

struct ContentView: View {
    @State private var pickerPresented = false
    @State private var originalDocument: PDFDocument?
    @State private var reflowedDocument: PDFDocument?
    @State private var showingReflow = false
    @State private var reflowing = false
    @State private var error: String?
    @State private var displayName = "PDF Reflow"
    @State private var settingsPresented = false

    @StateObject private var engine = ReflowEngine()
    @StateObject private var recents = RecentPDFsStore()
    @StateObject private var settings = AppSettings()

    private var displayed: PDFDocument? {
        showingReflow ? reflowedDocument : originalDocument
    }

    var body: some View {
        NavigationStack {
            ZStack {
                if let doc = displayed {
                    PDFViewer(document: doc)
                        .ignoresSafeArea(edges: .bottom)
                } else {
                    RecentsHomeView(
                        recents: recents,
                        onOpenRecent: openRecent
                    )
                    .safeAreaInset(edge: .bottom) {
                        BottomOpenBar { pickerPresented = true }
                    }
                }

                if reflowing {
                    ReflowingOverlay()
                }
            }
            .navigationTitle(navigationTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { toolbarContent }
            .fileImporter(
                isPresented: $pickerPresented,
                allowedContentTypes: [.pdf],
                allowsMultipleSelection: false,
                onCompletion: handlePicker
            )
            .sheet(isPresented: $settingsPresented) {
                SettingsView(settings: settings)
            }
            .alert("Error", isPresented: Binding(
                get: { error != nil },
                set: { if !$0 { error = nil } }
            )) {
                Button("OK") { error = nil }
            } message: {
                Text(error ?? "")
            }
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        if displayed == nil {
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    settingsPresented = true
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
                .accessibilityLabel("Settings")
            }
            ToolbarItem(placement: .topBarTrailing) {
                SortMenu(sort: $recents.sort)
            }
        } else {
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    closeDocument()
                } label: {
                    Label("Library", systemImage: "chevron.backward")
                }
                .accessibilityLabel("Back to library")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await toggleReflow() }
                } label: {
                    Label(
                        showingReflow ? "Original" : "Reflow",
                        systemImage: showingReflow
                            ? "doc.plaintext"
                            : "iphone.gen3"
                    )
                }
                .disabled(originalDocument == nil || reflowing)
                .accessibilityLabel(showingReflow ? "Show original" : "Reflow for mobile")
            }
        }
    }

    private var navigationTitle: String {
        guard originalDocument != nil else { return "PDF Reflow" }
        return showingReflow ? "\(displayName) — Reflowed" : displayName
    }

    private func handlePicker(_ result: Result<[URL], Error>) {
        switch result {
        case .failure(let err):
            error = err.localizedDescription
        case .success(let urls):
            guard let url = urls.first else { return }
            loadPDF(at: url)
        }
    }

    private func openRecent(_ item: RecentPDF) {
        guard let resolved = recents.resolve(item) else {
            error = "Couldn't locate \(item.name)."
            recents.remove(item)
            return
        }
        loadPDF(at: resolved.url)
    }

    private func loadPDF(at url: URL) {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }

        guard let data = try? Data(contentsOf: url),
              let doc = PDFDocument(data: data) else {
            error = "Could not read \(url.lastPathComponent)."
            return
        }
        originalDocument = doc
        reflowedDocument = nil
        showingReflow = false
        displayName = url.deletingPathExtension().lastPathComponent
        recents.record(url: url, size: Int64(data.count))
    }

    private func closeDocument() {
        originalDocument = nil
        reflowedDocument = nil
        showingReflow = false
        displayName = "PDF Reflow"
    }

    private func toggleReflow() async {
        guard let original = originalDocument else { return }

        if showingReflow {
            showingReflow = false
            return
        }
        if reflowedDocument != nil {
            showingReflow = true
            return
        }
        guard let data = original.dataRepresentation() else {
            error = "Could not serialize the loaded PDF."
            return
        }

        reflowing = true
        defer { reflowing = false }

        let preset = ReflowPreset(
            pageWidth: ReflowPreset.iphone17.pageWidth,
            pageHeight: ReflowPreset.iphone17.pageHeight,
            bodySize: settings.fontSize,
            figureDpi: settings.imagePPI
        )

        do {
            let reflowed = try await engine.reflow(pdfData: data, preset: preset)
            guard let doc = PDFDocument(data: reflowed) else {
                throw ReflowError.invalidResponse
            }
            reflowedDocument = doc
            showingReflow = true
        } catch {
            self.error = "Reflow failed: \(error.localizedDescription)"
        }
    }
}

private struct SortMenu: View {
    @Binding var sort: RecentsSort

    var body: some View {
        Menu {
            Picker("Sort", selection: $sort) {
                ForEach(RecentsSort.allCases) { option in
                    Text(option.label).tag(option)
                }
            }
        } label: {
            Label("Sort", systemImage: "arrow.up.arrow.down")
        }
        .accessibilityLabel("Sort recents")
    }
}

private struct RecentsHomeView: View {
    @ObservedObject var recents: RecentPDFsStore
    let onOpenRecent: (RecentPDF) -> Void

    var body: some View {
        Group {
            if recents.items.isEmpty {
                EmptyRecentsView()
            } else {
                List {
                    Section("Recent PDFs") {
                        ForEach(recents.sorted) { item in
                            Button {
                                onOpenRecent(item)
                            } label: {
                                RecentRow(item: item)
                            }
                            .buttonStyle(.plain)
                            .swipeActions {
                                Button(role: .destructive) {
                                    recents.remove(item)
                                } label: {
                                    Label("Remove", systemImage: "trash")
                                }
                            }
                        }
                    }
                }
                .listStyle(.insetGrouped)
            }
        }
    }
}

private struct RecentRow: View {
    let item: RecentPDF

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f
    }()

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "doc.richtext")
                .font(.title2)
                .foregroundStyle(.tint)
                .frame(width: 32)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                HStack(spacing: 6) {
                    Text(Self.dateFormatter.string(from: item.lastOpened))
                    Text("·")
                    Text(ByteCountFormatter.string(fromByteCount: item.size, countStyle: .file))
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
        .padding(.vertical, 4)
    }
}

private struct EmptyRecentsView: View {
    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "doc.richtext")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.secondary)
            Text("No recent PDFs")
                .font(.title3.weight(.medium))
            Text("Open a PDF to get started. Tap the reflow button later to switch to a single-column phone view.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}

private struct BottomOpenBar: View {
    let action: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Divider()
            HStack {
                Spacer()
                Button(action: action) {
                    Label("Open PDF", systemImage: "folder")
                        .font(.body.weight(.semibold))
                        .frame(minWidth: 200)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                Spacer()
            }
            .padding(.vertical, 12)
            .padding(.horizontal, 16)
        }
        .background(.bar)
    }
}

private struct ReflowingOverlay: View {
    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text("Reflowing…")
                .font(.callout.weight(.medium))
            Text("First run downloads the WASM Python runtime; later runs are quick.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 260)
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .shadow(radius: 12, y: 4)
    }
}

#Preview {
    ContentView()
}
