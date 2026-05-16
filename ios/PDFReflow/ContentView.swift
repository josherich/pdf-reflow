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

    @StateObject private var engine = ReflowEngine()

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
                    EmptyStateView { pickerPresented = true }
                }

                if reflowing {
                    ReflowingOverlay()
                }
            }
            .navigationTitle(navigationTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        pickerPresented = true
                    } label: {
                        Label("Open PDF", systemImage: "folder")
                    }
                    .accessibilityLabel("Open PDF")
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
            .fileImporter(
                isPresented: $pickerPresented,
                allowedContentTypes: [.pdf],
                allowsMultipleSelection: false,
                onCompletion: handlePicker
            )
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

    private var navigationTitle: String {
        guard originalDocument != nil else { return displayName }
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

        do {
            let reflowed = try await engine.reflow(pdfData: data)
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

private struct EmptyStateView: View {
    let action: () -> Void

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "doc.richtext")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.secondary)
            Text("Open a PDF to begin")
                .font(.title3.weight(.medium))
            Text("Tap the reflow button to switch to a single-column phone view.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 32)
            Button("Choose PDF", action: action)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
        }
        .padding()
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
