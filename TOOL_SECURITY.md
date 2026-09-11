# Tool installation security

`GithubToolCatalog` treats repository content as untrusted until each downloaded
file has been checked against integrity metadata returned by GitHub.

- All GitHub API and raw-content requests must use HTTPS. Redirects to HTTP are
  rejected.
- Catalogue responses, individual files, and complete tool packages have hard
  byte limits. The defaults are configurable on `GithubToolCatalog`.
- Repository paths must be canonical relative POSIX paths. Absolute paths,
  `..`, backslashes, NULs, and other traversal forms are rejected.
- Installation normally requires a valid Git blob SHA from the GitHub Trees API
  for every file. The downloaded bytes are checked using Git's blob object hash
  (`SHA-1("blob <size>\\0" + content)`) before the file is staged.
- A file without a Git blob SHA may instead use an expected SHA-256 from the
  package manifest, but only when that manifest itself was verified against its
  Git blob SHA. This preserves a chain of trust instead of accepting a
  self-attested digest from an unverified manifest.

Optional manifest fallback format:

```toml
[integrity.files]
"tool.py" = "sha256:<64 lowercase-or-uppercase hex characters>"
"assets/schema.json" = "sha256:<64 hex characters>"
```

Missing or invalid integrity metadata does not prevent catalogue browsing, but
automatic installation fails closed. Local directory and local ZIP installs are
handled separately by `ToolManager` and do not use this GitHub-specific chain.
