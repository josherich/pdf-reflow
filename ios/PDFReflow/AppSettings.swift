import Foundation

@MainActor
final class AppSettings: ObservableObject {
    private static let fontKey = "AppSettings.fontSize"
    private static let ppiKey = "AppSettings.imagePPI"

    static let fontRange: ClosedRange<Double> = 8...24
    static let ppiRange: ClosedRange<Double> = 72...300
    static let defaultFont: Double = 11
    static let defaultPPI: Double = 150

    @Published var fontSize: Double {
        didSet { UserDefaults.standard.set(fontSize, forKey: Self.fontKey) }
    }

    @Published var imagePPI: Double {
        didSet { UserDefaults.standard.set(imagePPI, forKey: Self.ppiKey) }
    }

    init() {
        let defaults = UserDefaults.standard
        let storedFont = defaults.object(forKey: Self.fontKey) as? Double
        let storedPPI = defaults.object(forKey: Self.ppiKey) as? Double
        self.fontSize = storedFont ?? Self.defaultFont
        self.imagePPI = storedPPI ?? Self.defaultPPI
    }
}
