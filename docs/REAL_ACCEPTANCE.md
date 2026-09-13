# Real acceptance release gate

Public real-toolchain evidence is release-specific. The normal release gate requires
`site/dist/real-evidence.json` to use `modelbake.public-acceptance.v3` and binds it
to the exact wheel and source distribution produced by that gate. It also requires
the warm manifest to be the exact current release in a verified baseline channel:
the terminal decision must be receipt-backed v2, the review must name a real
predecessor, and the candidate artifacts are live-checked again under the explicit
artifact root. Missing evidence, legacy v1 evidence, an initial-only decision, a
changed filename, digest, size, schema field, node record, receipt, or current
baseline fails the gate.

Use the explicit bootstrap mode only when the release archives must exist before a
fresh real acceptance run:

```sh
python3 scripts/release_check.py --bootstrap-real-acceptance
```

Bootstrap mode runs the ordinary source, fixture, archive, site, and wheel checks and
prepares `release-staging/packages/`, but deliberately skips public real-evidence
validation. Its success message says that it is not a final release gate.

Run the pinned real acceptance against that exact wheel, retaining the cold and warm
manifests and their artifact root privately. Then generate the reviewed, path-free
record against both exact bootstrap archives:

```sh
python3 scripts/generate_public_evidence.py \
  --cold-manifest /private/cold/manifest.json \
  --warm-manifest /private/warm/manifest.json \
  --artifact-root /private/artifacts \
  --baseline-store /private/baselines \
  --baseline-name production \
  --wheel release-staging/packages/modelbake_ai-0.1.0-py3-none-any.whl \
  --sdist release-staging/packages/modelbake_ai-0.1.0.tar.gz \
  --output site/dist/real-evidence.json
```

Generation exposes the baseline channel and digest identifiers for the current
decision, its consumed review, the exact candidate manifest, its predecessor, the
reviewed comparison, and the validated command shape. Local paths are replaced by
digest-bound placeholders. The private decision, review body, prompt text, logs,
and annotations stay out of the public JSON. This is a publisher-generated
compare-and-swap (CAS) acceptance. It records no actor identity, authorization,
independent attestation, or model-quality judgment.

Finally, rerun the normal gate without the bootstrap flag:

```sh
python3 scripts/release_check.py
```

Do not change release inputs between bootstrap, acceptance, and the final run. The
gate pins `SOURCE_DATE_EPOCH` while building so unchanged release inputs reproduce
the same wheel and sdist bytes. The final run still rejects the evidence if either
archive changes; regenerate evidence against the exact final archives rather than
weakening the binding.

The source distribution includes this guide and both release scripts, but excludes
the generated site and local `.integration` workspace.
