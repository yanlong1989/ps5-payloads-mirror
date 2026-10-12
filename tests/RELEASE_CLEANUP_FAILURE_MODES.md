# Release cleanup failure model

Exercise the actual mirror CLI through the HTTP fixture before enabling cleanup.

- Publishing a replacement must preserve the old file until metadata is committed.
- Cleanup must refuse a local catalog that differs from the committed repository catalog.
- Every current file must exist and match both size and SHA-256 before any deletion.
- Failed publication must preserve the currently referenced file.
- A catalog change during cleanup must stop remaining deletions.
- Cleanup may remove only unreferenced mirror-generated ELF/BIN assets uploaded by github-actions[bot].
- Manual uploads, unknown projects and other file types must remain untouched.
- Preview must list candidates without deleting files or modifying the Release description.
- HTTP deletion failure must be visible; the catalog and all current files must remain valid.
- Asset pagination must work when a Release contains more than 100 assets.
- Repeated cleanup must be idempotent and produce a verifiable JSON report.

Local evidence: reports/e2e-report.json. Verify live cleanup separately through a
completed GitHub Actions run and a fresh Release asset inventory.
