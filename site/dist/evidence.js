const state = document.querySelector("#evidence-load-state");

function short(value, length = 8) {
  return typeof value === "string" && value.length > length ? `${value.slice(0, length)}…` : value;
}

function bytes(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "unknown";
  if (value < 1_000) return `${value} B`;
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)} KB`;
  return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 1 : 2)} MB`;
}

function nodeBytes(node) {
  const values = Object.values(node.output_sizes_bytes || {});
  return values.length === 1 ? bytes(values[0]) : "unknown";
}

function nodeDigest(node) {
  const values = Object.values(node.output_digests || {});
  return values.length === 1 ? short(values[0], 15) : "unknown";
}

function text(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value;
}

function isDigest(value) {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
}

function renderNodes(nodes) {
  const container = document.querySelector("#evidence-node-rows");
  const fragment = document.createDocumentFragment();
  nodes.forEach((node) => {
    const row = document.createElement("div");
    row.className = "artifact-row";
    const name = document.createElement("strong");
    name.textContent = node.id;
    const observed = document.createElement("code");
    observed.textContent = node.state;
    if (node.state === "executed_on_runner") observed.className = "executed-text";
    const size = document.createElement("span");
    size.textContent = nodeBytes(node);
    const digest = document.createElement("small");
    digest.textContent = nodeDigest(node);
    row.append(name, observed, size, digest);
    fragment.append(row);
  });
  container.replaceChildren(fragment);
}

function renderCommands(commandRecord) {
  const container = document.querySelector("#evidence-command-list");
  const fragment = document.createDocumentFragment();
  commandRecord.nodes.forEach((record) => {
    const row = document.createElement("article");
    const header = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = record.node_id;
    const digest = document.createElement("small");
    digest.textContent = `tool ${short(record.tool_digest, 20)}`;
    header.append(label, digest);
    const command = document.createElement("code");
    command.textContent = record.display_argv.join(" ");
    row.append(header, command);
    fragment.append(row);
  });
  container.replaceChildren(fragment);
}

async function loadPublisherRecord() {
  try {
    const response = await fetch("./real-evidence.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const record = await response.json();
    const nodes = record.cold_run?.nodes;
    const commands = record.commands;
    const acceptance = record.acceptance;
    const acceptanceDigests = acceptance && [
      acceptance.decision_digest,
      acceptance.review_digest,
      acceptance.candidate_manifest_digest,
      acceptance.predecessor_decision_digest,
      acceptance.comparison_digest,
    ];
    if (
      record.schema !== "modelbake.public-acceptance.v3" ||
      !Array.isArray(nodes) ||
      nodes.length !== 6 ||
      commands?.path_policy !== "Local paths replaced with digest-bound placeholders." ||
      !Array.isArray(commands?.nodes) ||
      commands.nodes.length !== 5 ||
      commands.nodes.some((item) => !Array.isArray(item.display_argv)) ||
      acceptance?.kind !== "publisher-generated-cas-acceptance" ||
      typeof acceptance.channel !== "string" ||
      !acceptanceDigests.every(isDigest) ||
      acceptance.candidate_manifest_digest !== record.warm_run?.manifest?.modelbake_digest
    ) {
      throw new Error("the release record has an unexpected shape");
    }
    text("#evidence-cold-run", short(record.cold_run.run_id));
    text("#evidence-warm-run", short(record.warm_run?.run_id));
    text("#evidence-revision", record.runner?.observed_revision);
    text("#evidence-full-revision", record.runner?.observed_revision);
    text("#evidence-version", `modelbake-ai ${record.release?.version}`);
    text("#evidence-channel", acceptance.channel);
    text("#evidence-decision", acceptance.decision_digest);
    text("#evidence-review", acceptance.review_digest);
    text("#evidence-candidate", acceptance.candidate_manifest_digest);
    text("#evidence-predecessor", acceptance.predecessor_decision_digest);
    text("#evidence-comparison", acceptance.comparison_digest);
    text("#evidence-verified-count", `${record.cold_run?.digest_verification?.checked} / 6`);
    text("#evidence-wheel-hash", record.release?.wheel?.sha256 || "unavailable");
    text("#evidence-sdist-hash", record.release?.sdist?.sha256 || "unavailable");
    text("#evidence-headline", "Six outputs.");
    text("#evidence-subhead", "Six rechecked.");
    text("#evidence-summary", "This is the real build behind the downloadable ModelBake package. Six files were recorded and checked again. It proves the build ran; it does not certify model quality, safety, or production readiness.");
    renderNodes(nodes);
    renderCommands(commands);
    document.querySelectorAll(".record-bound").forEach((element) => { element.hidden = false; });
    state.textContent = "Exact package-bound record loaded.";
    state.classList.add("loaded");
  } catch (error) {
    state.textContent = `Record unavailable: ${error.message}`;
    state.classList.add("failed");
  }
}

loadPublisherRecord();
