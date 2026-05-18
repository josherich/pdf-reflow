import SwiftUI
import PDFKit

struct PDFViewer: UIViewRepresentable {
    let document: PDFDocument
    /// Bumped by the owner whenever ``document`` has been mutated in
    /// place (e.g. a streaming reflow appended new pages). The view
    /// refreshes on the next ``updateUIView`` so the new pages become
    /// visible without losing the user's scroll position.
    var revision: Int = 0

    func makeUIView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.usePageViewController(false)
        view.minScaleFactor = 0.25
        view.maxScaleFactor = 4.0
        view.backgroundColor = .systemBackground
        view.document = document
        context.coordinator.appliedRevision = revision
        return view
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator {
        var appliedRevision: Int = -1
    }

    func updateUIView(_ view: PDFView, context: Context) {
        let documentChanged = view.document !== document
        let revisionChanged = context.coordinator.appliedRevision != revision

        if !documentChanged && !revisionChanged {
            return
        }

        if documentChanged {
            // Preserve the current reading position when the underlying
            // document is swapped (e.g. preview → full reflow): capture
            // the page index in the old document, then jump to the same
            // index in the new one.
            let previousIndex: Int? = view.currentPage.flatMap { page in
                view.document?.index(for: page)
            }
            view.document = document
            if let idx = previousIndex,
               idx >= 0,
               idx < document.pageCount,
               let page = document.page(at: idx) {
                view.go(to: page)
            } else {
                view.scaleFactor = view.scaleFactorForSizeToFit
                view.goToFirstPage(nil)
            }
        } else {
            // In-place mutation. PDFView observes PDFKit's insert/remove
            // notifications, so layout updates on its own — we just nudge
            // it in case the document grew while off-screen.
            view.layoutDocumentView()
        }

        context.coordinator.appliedRevision = revision
    }
}
