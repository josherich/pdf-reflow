import Foundation

/// Learns how long a reflow takes so the modal can show a progress bar
/// instead of an indeterminate spinner.
///
/// Tracks two quantities, persisted in UserDefaults:
///   * `msPerPage` — average per-page processing time (EMA)
///   * `coldStartMs` — fixed overhead the first time the WebView/Pyodide
///     pipeline is touched in an app launch
///
/// Cold-start samples and warm samples are kept separate so a single cold
/// run does not skew the per-page average.
@MainActor
final class ReflowDurationEstimator {
    static let shared = ReflowDurationEstimator()

    private let msPerPageKey = "ReflowDuration.msPerPage"
    private let samplesKey = "ReflowDuration.samples"
    private let coldStartKey = "ReflowDuration.coldStartMs"

    private let defaultMsPerPage: Double = 400
    private let defaultColdStartMs: Double = 3_000

    private var msPerPage: Double {
        let v = UserDefaults.standard.double(forKey: msPerPageKey)
        return v > 0 ? v : defaultMsPerPage
    }

    private var coldStartMs: Double {
        let v = UserDefaults.standard.double(forKey: coldStartKey)
        return v > 0 ? v : defaultColdStartMs
    }

    func estimate(pageCount: Int, includeColdStart: Bool) -> TimeInterval {
        let pages = max(1, pageCount)
        let overhead = includeColdStart ? coldStartMs : 0
        return (overhead + msPerPage * Double(pages)) / 1000.0
    }

    func record(pageCount: Int, duration: TimeInterval, wasColdStart: Bool) {
        let pages = max(1, pageCount)
        let ms = duration * 1000.0
        if wasColdStart {
            let observedOverhead = max(0, ms - msPerPage * Double(pages))
            // Replace the default on the first cold sample; blend afterward.
            let previous = UserDefaults.standard.double(forKey: coldStartKey)
            let blended = previous > 0
                ? 0.5 * previous + 0.5 * observedOverhead
                : observedOverhead
            UserDefaults.standard.set(blended, forKey: coldStartKey)
        } else {
            let observed = ms / Double(pages)
            let samples = UserDefaults.standard.integer(forKey: samplesKey)
            // First sample replaces the default outright; later samples blend in.
            let weight = samples == 0 ? 1.0 : max(0.2, min(0.5, 1.0 / Double(samples + 1)))
            let blended = (1 - weight) * msPerPage + weight * observed
            UserDefaults.standard.set(blended, forKey: msPerPageKey)
            UserDefaults.standard.set(samples + 1, forKey: samplesKey)
        }
    }
}
