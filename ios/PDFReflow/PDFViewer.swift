import SwiftUI
import PDFKit

struct PDFViewer: UIViewRepresentable {
    let document: PDFDocument
    /// Bumped by the owner whenever ``document`` has been mutated in
    /// place (e.g. a streaming reflow appended new pages). The view
    /// refreshes on the next ``updateUIView`` so the new pages become
    /// visible without losing the user's scroll position.
    var revision: Int = 0
    /// Set by the owner to request a jump to a specific page index in
    /// the current ``document`` (e.g. from the table-of-contents modal).
    /// The view consumes the value by setting it back to `nil`.
    @Binding var pendingPageIndex: Int?
    /// Updated by the view whenever the visible page changes, so callers
    /// can drive UI like the TOC's "current section" highlight.
    var onPageChange: ((Int) -> Void)? = nil

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
        context.coordinator.attach(to: view, owner: self)
        return view
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator {
        var appliedRevision: Int = -1
        var onPageChange: ((Int) -> Void)?
        private weak var view: PDFView?
        private var observer: NSObjectProtocol?

        func attach(to view: PDFView, owner: PDFViewer) {
            self.view = view
            self.onPageChange = owner.onPageChange
            observer = NotificationCenter.default.addObserver(
                forName: .PDFViewPageChanged,
                object: view,
                queue: .main
            ) { [weak self] _ in
                guard let self,
                      let v = self.view,
                      let page = v.currentPage,
                      let idx = v.document?.index(for: page),
                      idx >= 0
                else { return }
                self.onPageChange?(idx)
            }
        }

        deinit {
            if let observer { NotificationCenter.default.removeObserver(observer) }
        }
    }

    func updateUIView(_ view: PDFView, context: Context) {
        context.coordinator.onPageChange = onPageChange
        let documentChanged = view.document !== document
        let revisionChanged = context.coordinator.appliedRevision != revision

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
        } else if revisionChanged {
            // In-place mutation. PDFView observes PDFKit's insert/remove
            // notifications, so layout updates on its own — we just nudge
            // it in case the document grew while off-screen.
            view.layoutDocumentView()
        }

        if documentChanged || revisionChanged {
            context.coordinator.appliedRevision = revision
        }

        if let idx = pendingPageIndex {
            if idx >= 0,
               idx < document.pageCount,
               let page = document.page(at: idx) {
                view.go(to: page)
            }
            // Consume the request so it doesn't replay on the next update.
            DispatchQueue.main.async {
                self.pendingPageIndex = nil
            }
        }
    }
}
