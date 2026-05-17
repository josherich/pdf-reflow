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
        if view.document !== document {
            view.document = document
            view.scaleFactor = view.scaleFactorForSizeToFit
            view.goToFirstPage(nil)
        }
    }
}
