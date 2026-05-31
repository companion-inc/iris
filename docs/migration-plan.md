# Open-Source Migration Plan

The goal is a local-first open-source Iris repo with no hosted deploy, hosted
auth, hosted search, hosted event broker, cloud device management, or remote
update system.

## Done In This Pass

- Root README now describes the local-first app.
- Root scripts no longer expose hosted deploy or cloud update commands.
- Default desktop/API scripts use local auth and local ports.
- Root `.env.example` is local-first.
- Transcript search no longer requires hosted embeddings or a managed
  vectorstore.
- The local API boots with SQLite by creating `.iris/iris.sqlite`.
- The active API Dockerfile was removed so the app no longer ships a hosted
  container path.
- Local auth is the only active API auth path.
- Hosted realtime event tokens were removed from the active API and desktop path.
- Voice and device docs no longer present hosted setup as the main path.
- The old app-store companion, public website, cloud infrastructure, and stale
  research doc have been deleted.

## Remaining Code Cleanup

1. Secrets
   - Store provider API keys in local app settings.

2. Packaging
   - Add a normal desktop app build path.
   - Add first-run setup for local database, keys, mic, speaker, and voice
     profile.

## Non-Goals

- No hosted deploy environment.
- No hosted companion API.
- No cloud-only companion pairing flow.
- No required managed database.
- No required vector database.
