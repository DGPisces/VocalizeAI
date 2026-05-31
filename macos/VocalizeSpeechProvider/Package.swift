// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VocalizeSpeechProvider",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "VocalizeSpeechProvider", targets: ["VocalizeSpeechProvider"])
    ],
    dependencies: [
        .package(url: "https://github.com/vapor/vapor.git", from: "4.115.0")
    ],
    targets: [
        .executableTarget(
            name: "VocalizeSpeechProvider",
            dependencies: [
                .product(name: "Vapor", package: "vapor")
            ]
        )
    ]
)
