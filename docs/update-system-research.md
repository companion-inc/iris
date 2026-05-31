# Update System Research

Date: 2026-05-31

## Finding

Iris has a GitHub release download, but the app did not show whether a newer release existed.

## Decision

Add an in-app update indicator that checks the public GitHub latest-release API and compares the latest release tag with the app's embedded `IRISReleaseTag`.

This is the correct immediate path because it gives the user a visible update signal and direct download action without pretending the app has a safe self-updater.

## Distribution Boundary

The correct full self-update system for a directly distributed macOS app is Sparkle with a signed appcast and signed update archive. Sparkle documents EdDSA-signed archives and appcast-based publishing for macOS app updates. Apple documents that Developer ID signing and notarization are the distribution trust path outside the Mac App Store.

Until Iris has that signed updater pipeline, the app should open the GitHub release download rather than silently replacing itself.

## Voice Runtime Decision

Keep the local Mac voice default on Deepgram Nova-3 English (`IRIS_STT_LANGUAGE=en`) because the working legacy desktop sidecar used English, while the Swift path had drifted to `multi`. Expose multilingual as an explicit Settings choice instead of making it the silent default.

## Sources

- GitHub REST API releases: `https://docs.github.com/en/rest/releases`
- Sparkle documentation: `https://sparkle-project.org/documentation/`
- Apple macOS distribution: `https://developer.apple.com/macos/distribution/`
- Apple notarization documentation: `https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution`
