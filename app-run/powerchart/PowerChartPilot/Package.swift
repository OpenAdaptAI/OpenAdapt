// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "PowerChartPilot",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "PowerChartPilot",
            path: "Sources/PowerChartPilot",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
