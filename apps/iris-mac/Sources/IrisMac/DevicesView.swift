import SwiftUI

struct DevicesView: View {
    @Environment(IrisAppState.self) private var appState

    var body: some View {
        ContentPage {
            HStack(alignment: .firstTextBaseline) {
                Text("Devices")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Text("\(appState.devices.count)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            if appState.devices.isEmpty {
                EmptyState(title: "No local devices.", subtitle: "Local desktop sessions appear here.")
            } else {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(appState.devices) { device in
                        DeviceRow(device: device)
                    }
                }
            }
        }
        .task {
            if appState.devices.isEmpty {
                await appState.refresh()
            }
        }
    }
}

private struct DeviceRow: View {
    var device: IrisDevice

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(device.displayName)
                        .font(.title3.weight(.medium))
                    Text(device.id)
                        .font(.caption.monospaced())
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Spacer()
                StatusPill(title: device.status.capitalized, running: device.status == "online" || device.status == "listening")
            }
            HStack(spacing: 14) {
                DeviceMeta(label: "Kind", value: device.kind)
                DeviceMeta(label: "Model", value: device.model ?? device.product ?? "Unknown")
                if let firmwareVersion = device.firmwareVersion {
                    DeviceMeta(label: "Firmware", value: firmwareVersion)
                }
                if let lastSeenAt = device.lastSeenAt {
                    DeviceMeta(label: "Seen", value: lastSeenAt.formatted(.relative(presentation: .named)))
                }
            }
        }
        .padding(16)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct DeviceMeta: View {
    var label: String
    var value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }
}
