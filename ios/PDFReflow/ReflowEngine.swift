import Foundation
import WebKit

enum ReflowError: LocalizedError {
    case bundleMissing
    case invalidResponse
    case engineFailed(String)

    var errorDescription: String? {
        switch self {
        case .bundleMissing:
            return "Reflow assets are missing from the app bundle."
        case .invalidResponse:
            return "The reflow engine returned an unexpected response."
        case .engineFailed(let message):
            return message
        }
    }
}

struct ReflowPreset {
    var pageWidth: Double
    var pageHeight: Double
    var bodySize: Double
    var figureDpi: Double

    static let iphone17 = ReflowPreset(
        pageWidth: 360, pageHeight: 640, bodySize: 11, figureDpi: 150
    )
}

@MainActor
final class ReflowEngine: NSObject, ObservableObject {

    /// True once the WebView has loaded and bridge.js has finished warming
    /// Pyodide. Used to decide whether a reflow estimate should include
    /// cold-start overhead.
    @Published private(set) var isReady: Bool = false

    private var webView: WKWebView?
    private var schemeHandler: BridgeSchemeHandler?

    /// The in-flight (or completed) initialization. Concurrent callers all
    /// await the same Task; once it succeeds we keep it around so subsequent
    /// `reflow` calls are zero-cost.
    private var initTask: Task<WKWebView, Error>?

    /// Set by `MessageProxy` once `bridge.js` signals readiness.
    fileprivate var readyContinuation: CheckedContinuation<Void, Error>?

    func reflow(
        pdfData: Data,
        preset: ReflowPreset = .iphone17,
        pageRange: Range<Int>? = nil
    ) async throws -> Data {
        let webView = try await ensureReady()

        var cfg: [String: Any] = [
            "page_width": preset.pageWidth,
            "page_height": preset.pageHeight,
            "body_size": preset.bodySize,
            "figure_dpi": preset.figureDpi,
        ]
        if let range = pageRange {
            cfg["page_start"] = range.lowerBound
            cfg["page_end"] = range.upperBound
        }
        let arguments: [String: Any] = [
            "b64": pdfData.base64EncodedString(),
            "cfg": cfg,
        ]

        let result: Any?
        do {
            result = try await webView.callAsyncJavaScript(
                "return await window.reflowBase64(b64, cfg);",
                arguments: arguments,
                in: nil,
                contentWorld: .page
            )
        } catch {
            throw ReflowError.engineFailed(error.localizedDescription)
        }

        guard let b64 = result as? String,
              let data = Data(base64Encoded: b64) else {
            throw ReflowError.invalidResponse
        }
        return data
    }

    private func ensureReady() async throws -> WKWebView {
        if let task = initTask {
            return try await task.value
        }
        let task = Task { try await initializeWebView() }
        initTask = task
        do {
            return try await task.value
        } catch {
            // Drop the failed task so the next call can retry.
            initTask = nil
            throw error
        }
    }

    private func initializeWebView() async throws -> WKWebView {
        guard let resourceDir = Bundle.main.url(
            forResource: "index", withExtension: "html", subdirectory: "ReflowBridge"
        )?.deletingLastPathComponent() else {
            throw ReflowError.bundleMissing
        }

        let handler = BridgeSchemeHandler(rootDirectory: resourceDir)
        self.schemeHandler = handler

        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(handler, forURLScheme: BridgeSchemeHandler.scheme)

        let controller = WKUserContentController()
        let proxy = MessageProxy(engine: self)
        controller.add(proxy, name: "ready")
        controller.add(proxy, name: "engineError")
        config.userContentController = controller

        let webView = WKWebView(frame: .zero, configuration: config)
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        self.webView = webView

        let url = URL(string: "\(BridgeSchemeHandler.scheme)://bridge/index.html")!

        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            self.readyContinuation = cont
            webView.load(URLRequest(url: url))
        }
        return webView
    }

    fileprivate func handleReady() {
        readyContinuation?.resume()
        readyContinuation = nil
        isReady = true
    }

    fileprivate func handleEngineError(_ message: String) {
        readyContinuation?.resume(throwing: ReflowError.engineFailed(message))
        readyContinuation = nil
    }
}

/// Weak-ref proxy so `WKUserContentController` doesn't strong-retain the engine.
private final class MessageProxy: NSObject, WKScriptMessageHandler {
    weak var engine: ReflowEngine?

    init(engine: ReflowEngine) { self.engine = engine }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        let name = message.name
        let body = message.body
        Task { @MainActor [weak engine] in
            guard let engine else { return }
            switch name {
            case "ready":
                engine.handleReady()
            case "engineError":
                engine.handleEngineError((body as? String) ?? "Unknown engine error")
            default:
                break
            }
        }
    }
}
