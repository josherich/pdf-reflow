import SwiftUI
import PDFKit

struct PDFViewer: UIViewRepresentable {
    let document: PDFDocument

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
        return view
    }

    func updateUIView(_ view: PDFView, context: Context) {
        guard view.document !== document else { return }

        // Preserve the current reading position when the underlying
        // document is swapped (e.g. preview → full reflow): capture the
        // page index in the old document, then jump to the same index
        // in the new one.
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
    }
}
