# 60-day moat experiment

ModelBake has no defensible moat on day one. The local engine is open, the category is crowded, and conversion commands are copyable. The bet is that repeated release execution can accumulate assets and workflow position that are harder to replace than the CLI.

## Potential moat layers

1. **Accepted-baseline history:** a structured, reviewable sequence of what changed between approved and candidate releases, which decision was made, and which exact evidence supported it.
2. **Outcome evidence:** a structured corpus connecting checkpoint/tool/runner fingerprints to observed success and failure on exact execution profiles.
3. **Digest-checked artifact cache hypothesis:** expensive artifacts reused across teams and builds only after byte checks and with policy-aware access.
4. **Execution fleet:** reproducible named hardware/runtime profiles that make new evidence comparable to prior evidence.
5. **Workflow embedding:** required comparisons and promotion records inside CI, registries, deployment systems, and incident response.

None exists at meaningful scale in v0. Lock-in is acceptable only when it comes from accumulated useful evidence and integration, while recipes and manifests remain exportable.

## Core hypothesis

If teams make ModelBake the automatic review step for repeated open-model releases, then each accepted baseline makes the next diff more useful while runs improve cache coverage, failure knowledge, and downstream audit history. That lowers marginal release cost and replacement becomes a history/data/integration migration, not a command substitution.

## 60-day test

Recruit 10 teams that already ran at least four qualifying model builds in the prior eight weeks. No more than four may come from founder-led warm introductions, and at least five must install from public documentation without a synchronous setup call. Record the incumbent workflow before introducing ModelBake; teams that merely agree to a design-partner cadence do not count as retained users.

Run a controlled comparison on the same source, targets, and hardware against each team's existing scripts or closest incumbent. Track, with permission:

- unattended CI executions per team and the distinct weeks in which they occur;
- setup and operator minutes, wall time, compute, storage, and egress for both workflows;
- cold-build and digest-checked repeat-build cache behavior, separated by private and shareable inputs;
- downstream decisions that consume a manifest, identified before the decision is known;
- silent false-green results, user misinterpretations, and adapter breakage across at least three upstream `llama.cpp` revisions;
- the work needed to export records and complete one build after the hosted component is removed;
- a signed, budget-owner-approved paid pilot rather than a non-binding statement of interest.

Interview operators after the first and third unprompted run. Ask what changed in a release decision and what they would replace ModelBake with; do not count satisfaction scores as retention or moat evidence.

## Separate pass criteria

### Utility gate

The local wedge merits continued maintenance only if, by day 60:

- at least 5 teams complete a real build without synchronous assistance, with median setup under two hours;
- at least 5 teams invoke ModelBake automatically in four distinct weeks without founder reminders;
- the controlled comparison shows at least 30% lower median operator time or total build cost;
- at least 4 teams use a manifest in a predeclared promotion, rollback, or debugging decision;
- there are zero silent false-green results and fewer than 10% of interviewed operators overread smoke evidence;
- upstream-revision breakage is detected automatically and repaired in a median of less than 48 hours.

### Moat and business gate

Hosted cache/fleet work proceeds only if the utility gate passes **and**:

- at least 3 independent budget owners sign paid pilots at $1,000 per month or more;
- at least 2 organizations safely reuse three or more opted-in, access-compatible nodes originally built by another organization;
- measured hosted compute, storage, egress, and support cost is below 35% of committed revenue;
- at least 3 teams choose ModelBake over their incumbent in a blinded or pre-scored workflow comparison;
- the removal test proves recipes and manifests remain exportable while users can name measurable time, compute, or evidence they lose without the service.

Passing only the utility gate validates an open-source tool, not a moat, high retention, or a business. Passing both gates is still evidence for a larger test—not proof of durable defensibility.

## Fail or pivot criteria

Stop expanding the platform if fewer than 5 teams run it automatically across four weeks, if most users extract commands and discard manifests, if no budget owner accepts the price, or if cache savings cannot exceed storage, egress, and maintenance cost. Pivot the wedge if real failures are dominated by model evaluation or deployment concerns that the recorded graph cannot inform.

Do not respond to weak retention by adding many formats. That would widen support cost without proving a compounding asset.

## Competitive falsification

For every design partner, compare the full workflow—not a feature checklist—against its current scripts and relevant alternatives such as Docker Model Runner, KitOps, Olive, Hugging Face Optimum, and NVIDIA model-serving tooling. The experiment fails if an existing system can deliver the same scoped evidence and workflow value with lower switching and operating cost.

## Data rights and portability

Any remote experiment must be explicitly opt-in and define ownership, retention,
isolation, deletion, and aggregation before collecting private checkpoint metadata,
prompts, logs, or artifacts. Participants must be able to export recipes, manifests,
and their own evidence. This experiment does not demonstrate a moat, and no value
hypothesis may depend on obscuring formats or trapping model weights.
