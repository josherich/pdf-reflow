import Foundation
import WebKit

/// Serves bundled assets to the WKWebView under a custom scheme so the page
/// avoids `file://` CORS quirks. Used by ReflowEngine.
final class BridgeSchemeHandler: NSObject, WKURLSchemeHandler {

    static let scheme = "pdfreflow"

    let rootDirectory: URL

    init(rootDirectory: URL) {
        self.rootDirectory = rootDirectory.standardizedFileURL
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        guard let url = urlSchemeTask.request.url else {
            urlSchemeTask.didFailWithError(URLError(.badURL))
            return
        }

        let path = url.path.hasPrefix("/") ? String(url.path.dropFirst()) : url.path
        let target = rootDirectory.appendingPathComponent(path).standardizedFileURL

        // Sandbox: never serve anything outside the bundled bridge directory.
        guard target.path.hasPrefix(rootDirectory.path) else {
            urlSchemeTask.didFailWithError(URLError(.noPermissionsToReadFile))
            return
        }

        do {
            let data = try Data(contentsOf: target)
            let response = HTTPURLResponse(
                url: url,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: [
                    "Content-Type": mimeType(for: target.pathExtension),
                    "Content-Length": String(data.count),
                    "Access-Control-Allow-Origin": "*",
                ]
            )!
            urlSchemeTask.didReceive(response)
            urlSchemeTask.didReceive(data)
            urlSchemeTask.didFinish()
        } catch {
            urlSchemeTask.didFailWithError(error)
        }
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {
        // No async work to cancel.
    }

    private func mimeType(for ext: String) -> String {
        switch ext.lowercased() {
        case "html", "htm": return "text/html; charset=utf-8"
        case "js":          return "application/javascript; charset=utf-8"
        case "css":         return "text/css; charset=utf-8"
        case "json":        return "application/json; charset=utf-8"
        case "py":          return "text/x-python; charset=utf-8"
        case "whl", "zip":  return "application/zip"
        case "wasm":        return "application/wasm"
        case "pdf":         return "application/pdf"
        default:            return "application/octet-stream"
        }
    }
}
