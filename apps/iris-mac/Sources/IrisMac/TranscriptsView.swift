import SwiftUI

struct TranscriptsView: View {
    @Environment(IrisAppState.self) private var appState

    var body: some View {
        ContentPage {
            Text("Transcripts")
                .font(.largeTitle.weight(.semibold))
            if appState.transcripts.isEmpty {
                EmptyState(title: "No transcripts yet.", subtitle: "Captured speech will appear here.")
            } else {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(appState.transcripts) { segment in
                        TranscriptRow(segment: segment)
                    }
                }
            }
        }
    }
}

struct TranscriptRow: View {
    var segment: TranscriptSegment
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(segment.speakerName ?? "Voice")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if let emotion = segment.emotionLabel {
                    Text(emotion)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(.quaternary, in: Capsule())
                }
                Spacer()
                if let startedAt = segment.startedAt {
                    Text(startedAt, style: .time)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }
            Text(segment.text)
                .font(compact ? .body : .title3)
                .lineLimit(compact ? 3 : nil)
        }
        .padding(compact ? 0 : 16)
        .background(compact ? Color.clear : Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }
}
