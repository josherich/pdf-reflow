import SwiftUI
import PDFKit
import UniformTypeIdentifiers

struct ContentView: View {
    @State private var pickerPresented = false
    @State private var originalDocument: PDFDocument?
    @State private var originalData: Data?
    @State private var originalSignature: String?
    @State private var reflowedDocument: PDFDocument?
    @State private var reflowedRevision = 0
    @State private var reflowedIsPartial = false
    @State private var showingReflow = false
    @State private var reflowing = false
    @State private var reflowProgress: Double = 0
    @State private var reflowProgressTimer: Timer?
    @State private var backgroundReflowing = false
    @State private var backgroundProgress: Double = 0
    @State private var backgroundTask: Task<Void, Never>?
    @State private var previewPageCount = 0
    @State private var totalPageCount = 0
    @State private var error: String?
    @State private var displayName = "PDF Reflow"
    @State private var settingsPresented = false
    @State private var tocPresented = false
    @State private var pendingPageIndex: Int?
    @State private var currentPageIndex: Int = 0

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
                    PDFViewer(
                        document: doc,
                        revision: showingReflow ? reflowedRevision : 0,
                        pendingPageIndex: $pendingPageIndex,
                        onPageChange: { currentPageIndex = $0 }
                    )
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
            .sheet(isPresented: $tocPresented) {
                if let doc = displayed {
                    TableOfContentsView(
                        document: doc,
                        currentPageIndex: currentPageIndex,
                        onSelect: { idx in
                            pendingPageIndex = idx
                        }
                    )
                }
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
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        tocPresented = true
                    } label: {
                        Label("Table of Contents", systemImage: "list.bullet.indent")
                    }
                    .disabled(displayed == nil)
                } label: {
                    Label("More", systemImage: "ellipsis.circle")
                }
                .accessibilityLabel("More options")
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
        cancelStreamingReflow()
        originalDocument = doc
        originalData = data
        originalSignature = ReflowCache.shared.signature(for: data)
        reflowedDocument = nil
        reflowedRevision = 0
        reflowedIsPartial = false
        showingReflow = false
        previewPageCount = 0
        totalPageCount = doc.pageCount
        displayName = url.deletingPathExtension().lastPathComponent
        recents.record(url: url, size: Int64(data.count), signature: originalSignature)
    }

    private func closeDocument() {
        cancelStreamingReflow()
        originalDocument = nil
        originalData = nil
        originalSignature = nil
        reflowedDocument = nil
        reflowedRevision = 0
        reflowedIsPartial = false
        showingReflow = false
        previewPageCount = 0
        totalPageCount = 0
        displayName = "PDF Reflow"
    }

    /// Chunk plan for streaming reflow. The first chunk is intentionally
    /// small (low time-to-first-page); follow-up chunks are larger so the
    /// per-chunk Pyodide round-trip overhead amortises. Tiny documents
    /// reflow in one shot — the chunk hop would just add latency.
    private func chunkPlan(for totalPages: Int) -> [Int] {
        if totalPages <= 5 { return [totalPages] }
        var sizes: [Int] = [min(3, totalPages)]
        var remaining = totalPages - sizes[0]
        let followUp = totalPages <= 20 ? 5 : 10
        while remaining > 0 {
            let take = min(followUp, remaining)
            sizes.append(take)
            remaining -= take
        }
        return sizes
    }

    private func toggleReflow() async {
        guard let originalDoc = originalDocument else { return }

        if showingReflow {
            showingReflow = false
            return
        }
        if reflowedDocument != nil {
            // Even if streaming hasn't finished yet, just show what we
            // have — more pages will append as they arrive.
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
            reflowedRevision &+= 1
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
        let plan = chunkPlan(for: pageCount)
        previewPageCount = plan.first ?? pageCount

        if plan.count == 1 {
            await runFullBlocking(
                data: data,
                preset: preset,
                cacheKey: cacheKey,
                pageCount: pageCount
            )
        } else {
            startStreamingReflow(
                data: data,
                preset: preset,
                cacheKey: cacheKey,
                plan: plan,
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
            reflowedRevision &+= 1
            reflowedIsPartial = false
            showingReflow = true
        } catch {
            self.error = "Reflow failed: \(error.localizedDescription)"
        }
    }

    /// Stream-reflow: reflow the source in N page-range chunks, growing
    /// the displayed document as each chunk arrives. The first chunk
    /// flips the UI to "showing reflow" and dismisses the blocking
    /// overlay; subsequent chunks append pages and update the bottom
    /// progress bar. When the last chunk lands we write the merged PDF
    /// (with offset-corrected outlines) to the cache so subsequent opens
    /// are instant.
    private func startStreamingReflow(
        data: Data,
        preset: ReflowPreset,
        cacheKey: String,
        plan: [Int],
        pageCount: Int
    ) {
        backgroundTask?.cancel()
        let wasColdStart = !engine.isReady
        let firstChunkSize = plan.first ?? pageCount
        let firstEstimated = ReflowDurationEstimator.shared.estimate(
            pageCount: firstChunkSize,
            includeColdStart: wasColdStart
        )

        reflowing = true
        startProgressAnimation(estimatedDuration: firstEstimated)

        let merged = PDFDocument()
        let totalStarted = Date()
        let firstStarted = Date()

        backgroundTask = Task { @MainActor in
            var rangeStart = 0
            var isFirstChunk = true

            for chunkSize in plan {
                let rangeEnd = min(rangeStart + chunkSize, pageCount)
                if Task.isCancelled { return }
                let chunkData: Data
                do {
                    chunkData = try await engine.reflow(
                        pdfData: data,
                        preset: preset,
                        pageRange: rangeStart..<rangeEnd
                    )
                } catch is CancellationError {
                    return
                } catch {
                    if isFirstChunk {
                        reflowing = false
                        stopProgressAnimation()
                    }
                    backgroundReflowing = false
                    self.error = "Reflow failed: \(error.localizedDescription)"
                    return
                }
                if Task.isCancelled { return }
                guard let chunkDoc = PDFDocument(data: chunkData) else {
                    if isFirstChunk {
                        reflowing = false
                        stopProgressAnimation()
                    }
                    backgroundReflowing = false
                    self.error = "Reflow produced an unreadable PDF chunk."
                    return
                }

                let base = merged.pageCount
                // Snapshot the chunk's outline BEFORE we move its pages
                // into `merged` — PDFKit's insert(_:at:) reparents pages,
                // so any after-the-fact lookup against `chunkDoc` would
                // return -1.
                let outlineSnapshot = Self.snapshotOutline(of: chunkDoc)
                for i in 0..<chunkDoc.pageCount {
                    if let page = chunkDoc.page(at: i) {
                        merged.insert(page, at: base + i)
                    }
                }
                Self.applyOutlineSnapshot(
                    outlineSnapshot, to: merged, pageOffset: base
                )

                if isFirstChunk {
                    ReflowDurationEstimator.shared.record(
                        pageCount: chunkSize,
                        duration: Date().timeIntervalSince(firstStarted),
                        wasColdStart: wasColdStart
                    )
                    reflowedDocument = merged
                    reflowedRevision &+= 1
                    reflowedIsPartial = rangeEnd < pageCount
                    showingReflow = true
                    reflowing = false
                    stopProgressAnimation()
                    if rangeEnd < pageCount {
                        backgroundReflowing = true
                        backgroundProgress = Double(rangeEnd) / Double(pageCount)
                    }
                    isFirstChunk = false
                } else {
                    reflowedRevision &+= 1
                    backgroundProgress = Double(rangeEnd) / Double(pageCount)
                }
                rangeStart = rangeEnd
            }

            reflowedIsPartial = false
            backgroundReflowing = false
            backgroundProgress = 1.0

            ReflowDurationEstimator.shared.record(
                pageCount: pageCount,
                duration: Date().timeIntervalSince(totalStarted),
                wasColdStart: wasColdStart
            )
            if let blob = merged.dataRepresentation() {
                ReflowCache.shared.write(key: cacheKey, data: blob)
            }
        }
    }

    private func cancelStreamingReflow() {
        backgroundTask?.cancel()
        backgroundTask = nil
        backgroundReflowing = false
        if reflowing {
            reflowing = false
            stopProgressAnimation()
        }
    }

    /// A POD snapshot of one outline node: enough to rebuild the entry
    /// against another document. We capture the source's page indices
    /// up-front because `PDFDocument.insert(_:at:)` reparents pages and
    /// a delayed `srcPage.document?.index(for:)` would return -1.
    private struct OutlineSnapshot {
        let label: String?
        let pageIndex: Int?
        let point: CGPoint
        let children: [OutlineSnapshot]
    }

    private static func snapshotOutline(of doc: PDFDocument) -> [OutlineSnapshot] {
        guard let root = doc.outlineRoot else { return [] }
        return snapshotChildren(of: root, in: doc)
    }

    private static func snapshotChildren(
        of node: PDFOutline, in doc: PDFDocument
    ) -> [OutlineSnapshot] {
        var out: [OutlineSnapshot] = []
        for i in 0..<node.numberOfChildren {
            guard let child = node.child(at: i) else { continue }
            var pageIndex: Int? = nil
            var point: CGPoint = .zero
            if let dest = child.destination, let page = dest.page {
                let idx = doc.index(for: page)
                if idx >= 0 { pageIndex = idx }
                point = dest.point
            }
            out.append(OutlineSnapshot(
                label: child.label,
                pageIndex: pageIndex,
                point: point,
                children: snapshotChildren(of: child, in: doc)
            ))
        }
        return out
    }

    /// Splice an outline snapshot into ``destination`` at ``pageOffset``,
    /// so chunk-local page indices (0, 1, …) map onto the right pages in
    /// the merged document. Nodes whose source page didn't make it into
    /// destination are dropped silently (their text content is already
    /// present — the bookmark is the only thing lost).
    private static func applyOutlineSnapshot(
        _ snapshot: [OutlineSnapshot],
        to destination: PDFDocument,
        pageOffset: Int
    ) {
        guard !snapshot.isEmpty else { return }
        let root = destination.outlineRoot ?? {
            let r = PDFOutline()
            destination.outlineRoot = r
            return r
        }()
        for node in snapshot {
            if let cloned = rebuildOutline(node, in: destination, pageOffset: pageOffset) {
                root.insertChild(cloned, at: root.numberOfChildren)
            }
        }
    }

    private static func rebuildOutline(
        _ node: OutlineSnapshot,
        in destination: PDFDocument,
        pageOffset: Int
    ) -> PDFOutline? {
        let copy = PDFOutline()
        copy.label = node.label
        if let local = node.pageIndex {
            let target = local + pageOffset
            if target >= 0,
               target < destination.pageCount,
               let dstPage = destination.page(at: target) {
                copy.destination = PDFDestination(page: dstPage, at: node.point)
            }
        }
        for child in node.children {
            if let cloned = rebuildOutline(child, in: destination, pageOffset: pageOffset) {
                copy.insertChild(cloned, at: copy.numberOfChildren)
            }
        }
        return copy
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
