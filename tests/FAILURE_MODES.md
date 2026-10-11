# End-to-end failure model

The verification launches the real command in an isolated directory against a
local HTTP server that implements upstream releases and GitHub release uploads.
It checks files, catalog links, release bytes, process exit codes, and reports.

- A fork could publish links to the original repository.
- A release could contain a PS4 binary, installer, helper, or several equally
  plausible files; an explicit pattern must remain authoritative for ZIP files.
- A prerelease could silently replace a stable release, or a draft could win.
- An asset could be replaced without changing its tag or URL.
- An unchanged run could download or upload every file again.
- A missing mirror asset could be mistaken for an up-to-date mirror.
- A timeout, transient HTTP error, truncated response, wrong digest, empty
  binary, invalid ELF, or ZIP mismatch could corrupt a previously valid file.
- An upload could fail after a successful download; catalog metadata must still
  point to the previous usable release asset.
- A successfully uploaded file could be published with the wrong size/digest.
- An ambiguous ZIP or a traversal member could select the wrong payload.
- An individual upstream failure could hide successful independent updates.
- README replacement could damage the surrounding prose or insert literal
  backslash-n strings.
- Re-running after a partial publication could fail on an existing asset name.
- More than 100 retained mirror assets could require API pagination.

The repeatable evidence is `reports/e2e-report.json`. Live network verification
and a successful GitHub Actions run are separate evidence boundaries.
