# Changelog

## 0.1.1 — Unreleased

- Notify interactive users when a newer stable ModelBake release is available.
- Cache update checks for 24 hours, skip CI and non-interactive runs, and allow
  the check to be disabled with `MODELBAKE_NO_UPDATE_CHECK=1`.
- Reuse completed tour children and resume an interrupted child when the same
  `--run-root` is used again.

## 0.1.0 — 2026-09-13

First public alpha.

- Build a local Llama safetensors checkpoint into GGUF F16, Q4_0, and Q8_0 with explicitly pinned local `llama.cpp` tools.
- Record a content-addressed graph, declared and observed tool identity, output digests, and scoped runner results.
- Reuse only cache entries whose recorded outputs still match their digests.
- Compare two release manifests without claiming that recorded files were re-read.
- Accept a verified manifest into a local, append-only, hash-chained baseline ledger and compare the next candidate with it.
- Run a credential-free built-in fixture demo from an installed wheel.
- Use the included trusted-runner GitHub Action with remote cache and evidence upload disabled by default.

The external `llama.cpp` adapter is POSIX-only in this release. ModelBake does not certify model quality, numerical equivalence, broad runtime compatibility, licensing, safety, security, or production readiness.
