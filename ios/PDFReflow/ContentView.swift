import SwiftUI
import PDFKit
import UniformTypeIdentifiers

struct ContentView: View {
    @State private var pickerPresented = false
    @State private var originalDocument: PDFDocument?
    @State private var originalData: Data?
    @State private var originalSignature: String?
    @State private var reflowedDocument: PDFDocument?
    @State private var reflowedIsPartial = false
    @State private var showingReflow = false
    @State private var reflowing = false
    @State private var reflowProgress: Double = 0
    @State private var reflowProgressTimer: Timer?
    @State private var backgroundReflowing = false
    @State private var backgroundProgress: Double = 0
    @State private var backgroundProgressTimer: Timer?
    @State private var backgroundTask: Task<Void, Never>?
    @State private var previewPageCount = 0
    @State private var totalPageCount = 0
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
                        .safeAreaInset(edge: .bottom) {
                            if showingReflow && backgroundReflowing {
                                BackgroundReflowBar(
                                    progress: backgroundProgress,
                                    previewCount: previewPageCount,
                                    totalPages: totalPageCount
                                )
                            }
                        }
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
                    ReflowingOverlay(progress: reflowProgress)
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
        cancelBackgroundReflow()
        originalDocument = doc
        originalData = data
        originalSignature = ReflowCache.shared.signature(for: data)
        reflowedDocument = nil
        reflowedIsPartial = false
        showingReflow = false
        previewPageCount = 0
        totalPageCount = doc.pageCount
        displayName = url.deletingPathExtension().lastPathComponent
        recents.record(url: url, size: Int64(data.count), signature: originalSignature)
    }

    private func closeDocument() {
        cancelBackgroundReflow()
        originalDocument = nil
        originalData = nil
        originalSignature = nil
        reflowedDocument = nil
        reflowedIsPartial = false
        showingReflow = false
        previewPageCount = 0
        totalPageCount = 0
        displayName = "PDF Reflow"
    }

    /// Number of pages to render up-front when the user taps Reflow. Returns
    /// nil for tiny documents where the full reflow is fast enough that
    /// the preview hop just adds latency.
    private func previewCount(for totalPages: Int) -> Int? {
        switch totalPages {
        case ...5: return nil
        case 6...10: return 3
        case 11...20: return 5
        default: return 10
        }
    }

    private func toggleReflow() async {
        guard let originalDoc = originalDocument else { return }

        if showingReflow {
            showingReflow = false
            return
        }
        if reflowedDocument != nil {
            // Even if we only have the partial preview so far, just show
            // what we have — the background full reflow keeps running.
            showingReflow = true
            return
        }
        guard let data = originalData ?? originalDoc.dataRepresentation() else {
            error = "Could not serialize the loaded PDF."
            return
        }

        let signature = originalSignature ?? ReflowCache.shared.signature(for: data)
        let cacheKey = ReflowCache.shared.key(
            signature: signature,
            fontSize: settings.fontSize,
            ppi: settings.imagePPI
        )

        if let cached = ReflowCache.shared.read(key: cacheKey),
           let doc = PDFDocument(data: cached) {
            reflowedDocument = doc
            reflowedIsPartial = false
            showingReflow = true
            return
        }

        let pageCount = originalDoc.pageCount
        let preset = ReflowPreset(
            pageWidth: ReflowPreset.iphone17.pageWidth,
            pageHeight: ReflowPreset.iphone17.pageHeight,
            bodySize: settings.fontSize,
            figureDpi: settings.imagePPI
        )

        totalPageCount = pageCount
        previewPageCount = previewCount(for: pageCount) ?? pageCount

        if let preview = previewCount(for: pageCount), preview < pageCount {
            await runPreviewThenBackground(
                data: data,
                preset: preset,
                cacheKey: cacheKey,
                previewCount: preview,
                totalPages: pageCount
            )
        } else {
            await runFullBlocking(
                data: data,
                preset: preset,
                cacheKey: cacheKey,
                pageCount: pageCount
            )
        }
    }

    private func runFullBlocking(
        data: Data,
        preset: ReflowPreset,
        cacheKey: String,
        pageCount: Int
    ) async {
        let wasColdStart = !engine.isReady
        let estimated = ReflowDurationEstimator.shared.estimate(
            pageCount: pageCount,
            includeColdStart: wasColdStart
        )

        reflowing = true
        startProgressAnimation(estimatedDuration: estimated)
        let startedAt = Date()
        defer {
            reflowing = false
            stopProgressAnimation()
        }

        do {
            let reflowed = try await engine.reflow(pdfData: data, preset: preset)
            ReflowDurationEstimator.shared.record(
                pageCount: pageCount,
                duration: Date().timeIntervalSince(startedAt),
                wasColdStart: wasColdStart
            )
            ReflowCache.shared.write(key: cacheKey, data: reflowed)
            guard let doc = PDFDocument(data: reflowed) else {
                throw ReflowError.invalidResponse
            }
            reflowedDocument = doc
            reflowedIsPartial = false
            showingReflow = true
        } catch {
            self.error = "Reflow failed: \(error.localizedDescription)"
        }
    }

    private func runPreviewThenBackground(
        data: Data,
        preset: ReflowPreset,
        cacheKey: String,
        previewCount: Int,
        totalPages: Int
    ) async {
        let wasColdStart = !engine.isReady
        let previewEstimated = ReflowDurationEstimator.shared.estimate(
            pageCount: previewCount,
            includeColdStart: wasColdStart
        )

        reflowing = true
        startProgressAnimation(estimatedDuration: previewEstimated)
        let previewStartedAt = Date()

        do {
            let preview = try await engine.reflow(
                pdfData: data,
                preset: preset,
                pageRange: 0..<previewCount
            )
            ReflowDurationEstimator.shared.record(
                pageCount: previewCount,
                duration: Date().timeIntervalSince(previewStartedAt),
                wasColdStart: wasColdStart
            )
            guard let previewDoc = PDFDocument(data: preview) else {
                throw ReflowError.invalidResponse
            }
            reflowedDocument = previewDoc
            reflowedIsPartial = true
            showingReflow = true
        } catch {
            self.error = "Reflow failed: \(error.localizedDescription)"
            reflowing = false
            stopProgressAnimation()
            return
        }

        reflowing = false
        stopProgressAnimation()

        startBackgroundFullReflow(
            data: data,
            preset: preset,
            cacheKey: cacheKey,
            totalPages: totalPages
        )
    }

    private func startBackgroundFullReflow(
        data: Data,
        preset: ReflowPreset,
        cacheKey: String,
        totalPages: Int
    ) {
        backgroundTask?.cancel()
        backgroundReflowing = true
        let estimated = ReflowDurationEstimator.shared.estimate(
            pageCount: totalPages,
            includeColdStart: false
        )
        startBackgroundProgressAnimation(estimatedDuration: estimated)

        backgroundTask = Task { @MainActor in
            let started = Date()
            defer {
                backgroundReflowing = false
                stopBackgroundProgressAnimation()
            }
            do {
                let full = try await engine.reflow(pdfData: data, preset: preset)
                try Task.checkCancellation()
                ReflowDurationEstimator.shared.record(
                    pageCount: totalPages,
                    duration: Date().timeIntervalSince(started),
                    wasColdStart: false
                )
                ReflowCache.shared.write(key: cacheKey, data: full)
                guard let doc = PDFDocument(data: full) else {
                    throw ReflowError.invalidResponse
                }
                // Only swap in if the user is still on the same source PDF.
                // (closeDocument / loadPDF cancel us, so reaching here means
                // we're still relevant.)
                reflowedDocument = doc
                reflowedIsPartial = false
            } catch is CancellationError {
                // user moved on; drop the result.
            } catch {
                self.error = "Full reflow failed: \(error.localizedDescription)"
            }
        }
    }

    private func cancelBackgroundReflow() {
        backgroundTask?.cancel()
        backgroundTask = nil
        backgroundReflowing = false
        stopBackgroundProgressAnimation()
    }

    private func startProgressAnimation(estimatedDuration: TimeInterval) {
        reflowProgressTimer?.invalidate()
        reflowProgress = 0
        let started = Date()
        // Asymptote at ~0.97 so the bar never reaches 100% before the actual
        // result is back — it then snaps to 1.0 in `stopProgressAnimation`.
        let ceiling = 0.97
        let duration = max(estimatedDuration, 1.0)
        reflowProgressTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
            Task { @MainActor in
                let elapsed = Date().timeIntervalSince(started)
                let fraction = elapsed / duration
                reflowProgress = ceiling * (1.0 - exp(-3.0 * fraction))
            }
        }
    }

    private func stopProgressAnimation() {
        reflowProgressTimer?.invalidate()
        reflowProgressTimer = nil
        reflowProgress = 1.0
    }

    private func startBackgroundProgressAnimation(estimatedDuration: TimeInterval) {
        backgroundProgressTimer?.invalidate()
        backgroundProgress = 0
        let started = Date()
        let ceiling = 0.97
        let duration = max(estimatedDuration, 1.0)
        backgroundProgressTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { _ in
            Task { @MainActor in
                let elapsed = Date().timeIntervalSince(started)
                let fraction = elapsed / duration
                backgroundProgress = ceiling * (1.0 - exp(-3.0 * fraction))
            }
        }
    }

    private func stopBackgroundProgressAnimation() {
        backgroundProgressTimer?.invalidate()
        backgroundProgressTimer = nil
        backgroundProgress = 1.0
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
                                    if let sig = item.signature {
                                        ReflowCache.shared.removeAll(signature: sig)
                                    }
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

private struct BackgroundReflowBar: View {
    let progress: Double
    let previewCount: Int
    let totalPages: Int

    var body: some View {
        let clamped = max(0, min(progress, 1))
        let remaining = max(0, totalPages - previewCount)
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.caption)
                Text("Reflowing remaining \(remaining) page\(remaining == 1 ? "" : "s")…")
                    .font(.caption)
                Spacer()
                Text("\(Int(clamped * 100))%")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: clamped)
                .progressViewStyle(.linear)
                .animation(.easeOut(duration: 0.2), value: clamped)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

private struct ReflowingOverlay: View {
    let progress: Double

    var body: some View {
        let clamped = max(0, min(progress, 1))
        VStack(spacing: 12) {
            ProgressView(value: clamped)
                .progressViewStyle(.linear)
                .frame(width: 220)
                .animation(.easeOut(duration: 0.15), value: clamped)
            Text("Reflowing… \(Int(clamped * 100))%")
                .font(.callout.weight(.medium))
                .monospacedDigit()
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .shadow(radius: 12, y: 4)
    }
}

#Preview {
    ContentView()
}
