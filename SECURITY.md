# Security policy

ModelBake executes local model tooling and handles potentially valuable checkpoint bytes. Please treat security reports carefully.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting from the repository **Security** tab. Include the affected revision, platform, minimal reproduction, impact, and whether untrusted recipe or filesystem content is involved. Do not open a public issue for a suspected vulnerability. If private reporting is unavailable, contact the repository owners through GitHub without disclosing exploit details publicly.

The maintainers will acknowledge a complete report when available, investigate it, and coordinate disclosure after a fix or documented mitigation. No response-time guarantee is made for this alpha project.

## Supported versions

Until a stable release exists, only the latest revision on the default branch is supported with security fixes. Published alpha versions may require upgrading rather than receiving backports.

## Security boundaries

The v0 recipe parser reduces several avoidable risks:

- only local sources beneath an allowed root are accepted;
- source traversal and source-tree symlinks are rejected;
- pickle-backed model files and `trust_remote_code` are rejected;
- recipes cannot provide commands or arbitrary arguments;
- real adapter tools must be explicit existing absolute files;
- child processes use argv arrays with `shell=False`, timeouts, and capped captured output;
- cache reads and manifest verification recompute recorded digests.

These controls do not make ModelBake a sandbox. Configured Python, converter, quantizer, and runner executables are trusted code with the invoking user's permissions. Local source bytes can exercise parser bugs in those tools. A user with filesystem access can replace tools or inputs before a run, exhaust disk or memory, or read manifests and logs. Smoke prompts and runner output may contain sensitive data. ModelBake does not verify model licenses, signatures, publisher identity, model safety, generated content, or the semantic correctness of a tool revision string.

For sensitive workloads, use an isolated host or container with minimal filesystem access, pin and independently verify tool sources, inspect licenses, avoid secrets in prompts, and retain run/cache directories according to your data policy.
