# Put ModelBake in the release review

ModelBake's recurring job is not “convert this file once.” It is to make the next
model release reviewable: which inputs changed, which recorded output digests changed,
which runner evidence is fresh, and which records came from digest-checked cache entries.

## Compare two release records

```bash
modelbake compare path/to/approved/manifest.json path/to/candidate/manifest.json
modelbake compare path/to/approved/manifest.json path/to/candidate/manifest.json --markdown
modelbake compare path/to/approved/manifest.json path/to/candidate/manifest.json --json
```

The comparison distinguishes a recorded output-digest change from an observation-only
change and from cache reuse. It never declares a candidate safe to promote.

## Ten-minute GitHub Actions setup

The copyable route vendors the reviewed ModelBake source into the consuming
repository. It has no unresolved action-owner placeholder and does not depend on a
ModelBake repository being public. From the consuming repository, with the v0.1.0
source archive in the current directory:

```bash
mkdir -p .github/actions/modelbake .github/workflows
tar -xzf modelbake_ai-0.1.0.tar.gz --strip-components=1 -C .github/actions/modelbake
cp .github/actions/modelbake/examples/github/modelbake-release.yml .github/workflows/modelbake-release.yml
cp .github/actions/modelbake/examples/modelbake.llamacpp.yaml modelbake.yaml
```

Edit only the machine-specific values in `modelbake.yaml`: `source.path`, the four
tool paths, and the pinned `runner.tools.revision`. Then define these GitHub
repository variables once; each must be an absolute path on every runner carrying
the `modelbake` label:

| Variable | Example | Kept across releases |
| --- | --- | --- |
| `MODELBAKE_SOURCE_ROOT` | `/srv/models` | mounted checkpoint root |
| `MODELBAKE_CACHE_DIRECTORY` | `/srv/modelbake/cache` | digest-checked build cache |
| `MODELBAKE_BASELINE_STORE` | `/srv/modelbake/baselines` | accepted-release ledger |

Commit `.github/actions/modelbake`, `.github/workflows/modelbake-release.yml`, and
`modelbake.yaml`. After that, **Actions → Review model release → Run workflow** is
the recurring entry point. Select `initial_baseline` only for the first release of
the `production` channel. Select `record_acceptance` only when the repository's own
release policy authorizes this run to consume the exact generated receipt.

The sample serializes production-channel runs. It keeps the cache and accepted
ledger outside the checkout so `actions/checkout` cannot remove the release memory.
Change the hard-coded `production` name and concurrency group together if the
repository has multiple release channels.

## Data transfer remains opt-in

The local CLI does not upload evidence or use a remote cache. The supplied action
also keeps both local by default. Setting `enable-github-cache: "true"` sends cache
content to GitHub; setting `upload-evidence: "true"` sends the generated evidence as
a run artifact. Review repository permissions, retention, artifact sensitivity, and
GitHub's terms before enabling either input.

The repository-root [`action.yml`](../action.yml) is a composite action that:

1. optionally restores ModelBake's content-addressed cache from GitHub's cache service;
2. installs the reviewed action source;
3. builds and live-verifies the configured recipe against its explicit run-artifact root;
4. renders the self-contained evidence report;
5. requires a local baseline store and creates an exact, predecessor-bound review receipt;
6. writes the Markdown comparison into the job summary;
7. optionally uploads the manifest, report, comparison, and receipt as GitHub run artifacts.

The v0 real adapter is intended for a labelled self-hosted runner that already has
the local checkpoint and pinned `llama.cpp` toolchain. ModelBake does not download
model weights or execute recipe-supplied commands. Teams that later replace the
vendored action with a remote `owner/repository@ref` should pin an immutable commit
they reviewed rather than a moving branch.

This is a copyable integration example, not native CI, a hosted ModelBake service,
or evidence that the workflow has been adopted in production. External `llama.cpp`
execution in v0 is POSIX-only.

## Baseline handling

For a channel's first release, set `initial-baseline: true`; ModelBake refuses that
flag once the channel has a decision. On later releases it refuses project changes
and binds the exact current decision as the receipt predecessor. After your own
release policy passes, consume exactly that receipt:

```bash
modelbake baseline accept "$MANIFEST" --store "$STORE" --name production \
  --allow-artifact-root "$CACHE" --review "$REVIEW"
modelbake baseline check "$MANIFEST" --store "$STORE" --name production \
  --allow-artifact-root "$CACHE"
```

If a competing release was accepted or any candidate/artifact bytes changed after
review, acceptance fails and a new review is required. This closes the gap between
“the diff CI displayed” and “the bytes the release ledger recorded.” It does not
claim that a person approved the release.

Treat an approved baseline manifest as release evidence, not as a credential.
Sanitize private prompts, paths, and logs before publishing it. The current CLI
digest-redacts smoke prompts in recorded argv, but generated stdout/stderr and
machine-local artifact paths can still be sensitive. Keep private evidence in the
same access-controlled artifact store as the release it governs.

An unchanged digest says only that the recorded bytes are identical. A cache hit
says those bytes were rehashed before reuse. Neither assertion proves model quality,
compatibility on a different runner, or promotion safety.
