// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "IrisMac",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "Iris", targets: ["IrisMac"])
    ],
    targets: [
        .executableTarget(
            name: "IrisMac",
            path: "Sources/IrisMac",
            linkerSettings: [
                .linkedLibrary("sqlite3")
            ]
        ),
        .testTarget(
            name: "IrisMacTests",
            dependencies: ["IrisMac"],
            path: "Tests/IrisMacTests",
            linkerSettings: [
                .linkedFramework("Testing")
            ]
        )
    ]
)
