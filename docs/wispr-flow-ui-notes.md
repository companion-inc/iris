# Wispr Flow UI Notes

This is based on the installed macOS app bundle at `/Applications/Wispr Flow.app`, its extracted Electron `app.asar`, bundled assets, migrations, and the local `flow.sqlite` schema. I did not find a clean source checkout on this machine; the renderer code is webpacked/minified, so these notes separate verified structure from product principles we can safely apply to Iris.

## Verified Structure

- Wispr Flow is split into focused renderer windows: `hub`, `status`, `overlay`, `scratchpad`, `meeting_recorder`, `calendar_reminder`, and `contextMenu`.
- The local database treats user output as first-class objects: `History`, `Dictionary`, `Meetings`, `Notes`, `Links`, and related version/image tables.
- Transcript history is central. `History` stores ASR text, formatted text, edited text, app/url context, timing, language, microphone device, conversation id, pasted text, and correction metadata.
- The bundle includes direct sound assets for dictation start, dictation stop, paste, notification, and achievement states.
- The renderer defines a cohesive design system: shared neutral tokens, reset styles, button resets, typography tokens, and reusable windows/components rather than independent per-screen styling.
- Settings and setup are heavily sectioned by task: account, team, microphone, language, shortcuts, connectors, dictionary/snippets, experiments, and confirmation dialogs.
- Setup/configuration actions use focused flows and dialogs instead of exposing every control in the main surface at once.

## Transferable Principles For Iris

- Home should be the live work surface. For Iris, that means the current transcript and recent conversation belong on Home, not hidden behind status cards.
- Status is supporting metadata. The user should see `Iris`, the live listening state, and the transcript first; runtime endpoints, provider status, and service details belong behind Settings or Details.
- Separate object types should stay separate. Conversations, Transcripts, Activity, Devices, and Settings can be separate tabs, while Home shows only the active voice loop and latest output.
- Use dialogs for focused setup. API keys and profile creation are configuration tasks; they should open in focused panels instead of crowding Settings or Voice by default.
- Buttons should read as commands, not icon collections. Keep icons for navigation, avatars, and passive status where useful; avoid icons inside routine action buttons.
- Lists should be calm rows. Repeated data should look like rows with one title, one secondary line, and one optional action area, not stacked colorful cards.
- Empty states should be short. Avoid explaining the whole product in empty space; say what is missing and let the surface stay quiet.
- Details should be collapsible. Diagnostics, local runtime URLs, service health, and provider wiring are important, but they should not compete with the primary conversation.
- Audio affordances should be explicit. Since Iris is always listening unless muted, the main command should be `Mute`/`Unmute`; do not make the user think they must manually start a session.
- Visual hierarchy should use space, type, and row grouping more than color. Iris should stay mostly white, with subdued borders and one clear active state.

## Iris Application

- Home becomes a transcript-first surface: title, listening state, mute control, current transcript, and latest conversation.
- Voice becomes a compact runtime/profile page with details collapsed.
- Settings exposes rows first and pushes API keys into a dialog.
- Content/action buttons are text-only except specialized icon-only affordances such as password reveal.
- Diagnostics remain available but lower in the hierarchy.

## Current Competitive Read, May 2026

- VoiceOS leads with a command outcome, not audio decoration: "Say it and it's done", then separates Agent Mode from Dictation Mode. The visible examples are app actions and composed text, not meters.
- VoiceOS also puts privacy/status controls in a settings-like surface: transcript saving, cloud audio storage, training, diagnostics. Iris should keep runtime detail available but out of the Home hierarchy.
- Wispr Flow's public positioning is speed and cross-app writing. Its product pages compare typing output to Flow output and show the resulting text as the hero object.
- Wispr Flow support docs route audio troubleshooting to Recent Activity/history rather than a large persistent live audio meter. That supports treating diagnostics as secondary to transcript/result.
- Willow's product flow is three steps: press a hotkey, speak naturally, perfect text appears. It sells "works anywhere" and voice commands through final text examples, not dense dashboard widgets.
- Willow's useful differentiator for Iris is comfort with quiet speech/background noise, but the UI lesson is still restraint: the interface should make the captured text feel spacious and primary.
- Granola desktop app lesson: its actual macOS app uses a collapsed rail/sidebar, centered work surface, one primary composer/note object, calm recent rows, and small recipe/workflow chips. The transferable pattern is structure and restraint, not copying Granola's dark visual skin.

## Immediate Iris Fix

- Remove the Home equalizer. It reads as generic audio software and fights the transcript.
- Replace it with a tiny state dot beside the listening label.
- Give the transcript more width, more minimum height, and more breathing room before the Recent section.
- Keep Mute/Refresh in the header, but let the live transcript own the screen.
- Move the whole desktop app away from a dashboard: narrow icon rail, document-like Home, flat rows, and runtime diagnostics pushed to secondary panels. Keep Iris visually closer to Wispr/VoiceOS/Willow: light, fast, and voice-utility native.
