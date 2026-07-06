// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "HermesVoice",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "HermesVoice", path: "Sources/HermesVoice")
    ]
)
