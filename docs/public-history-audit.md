# Public History Audit

Date: 2026-05-31

## Decision

The repository is being published from a clean root commit instead of preserving the private development history.

## Evidence

- Secret-shaped scans of the current tree and reachable history found development placeholders, not production credentials.
- Offensive-term scans of the reachable history found no matches.
- The existing history still contained public-unfriendly operational context: old deployment workflows, infrastructure files, private workspace paths, debug logs, and retired product names.
- GitHub's sensitive-data guidance says deleting a file in a later commit does not remove it from repository history. Rewriting public references is the correct cleanup action before treating the repository as public.

## Cleanup

- Removed the secret-management project metadata file from the public tree.
- Removed local voice-debug logs from the public tree.
- Replaced private absolute paths in tests and docs with repo-relative or user-neutral references.
- Removed retired OpenHome wording from the Bluetooth readiness helper.

## Verification

Before pushing the clean public history, run:

```sh
pnpm typecheck
pnpm --filter @iris/site build
cd apps/iris-mac && xcrun swift test --disable-sandbox --scratch-path /private/tmp/iris-mac-build
```

After pushing, verify that the public `main` branch has one commit and that branch/tag references no longer point at the private-era history.
