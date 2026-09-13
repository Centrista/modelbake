const nodes = {
  source: { title: "Fixture bundle", kind: "source", state: "produced", digest: "loading", output: "245 bytes", command: "In-process fixture adapter; no subprocess was launched." },
  f16: { title: "Fixture · F16", kind: "convert", state: "produced", digest: "loading", output: "444 bytes", command: "In-process fixture byte transform; this output is not GGUF." },
  q4: { title: "Fixture · Q4_0", kind: "quantize", state: "produced", digest: "loading", output: "547 bytes", command: "In-process deterministic transform; this output is not a quantized model." },
  q8: { title: "Fixture · Q8_0", kind: "quantize", state: "produced", digest: "loading", output: "547 bytes", command: "In-process deterministic transform; this output is not a quantized model." },
  "run-f16": { title: "Fixture observe · F16", kind: "smoke", state: "fixture_observed", digest: "loading", output: "4 synthetic tokens", command: "In-process fixture decode; no llama.cpp process was launched." },
  "run-q4": { title: "Fixture observe · Q4_0", kind: "smoke", state: "fixture_observed", digest: "loading", output: "4 synthetic tokens", command: "In-process fixture decode; no llama.cpp process was launched." },
  "run-q8": { title: "Fixture observe · Q8_0", kind: "smoke", state: "fixture_observed", digest: "loading", output: "4 synthetic tokens", command: "In-process fixture decode; no llama.cpp process was launched." }
};

const stages = [
  { ids: ["source"], label: "digesting source", message: "Source tree hashed; recipe and runner captured." },
  { ids: ["f16"], label: "replaying conversion", message: "The fixture adapter recorded a 444-byte F16-tagged test artifact." },
  { ids: ["q4", "q8"], label: "quantizing targets", message: "Q4_0 and Q8_0 target artifacts produced." },
  { ids: ["run-f16", "run-q4", "run-q8"], label: "replaying observations", message: "The manifest records 4 synthetic tokens for each of three fixture targets." }
];

const surface = document.querySelector(".build-surface");
const runButton = document.querySelector("#run-build");
const buildStatus = document.querySelector("#build-status");
const logStep = document.querySelector("#log-step");
const logMessage = document.querySelector("#log-message");
const drawer = document.querySelector("#node-drawer");
const drawerTitle = document.querySelector("#drawer-title");
const drawerDetails = document.querySelector("#drawer-details");
const drawerCommand = document.querySelector("#drawer-command");
let selectedNode = "source";
let running = false;
let priorFocus = null;

function wait(milliseconds) { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }

function selectNode(id, open = false) {
  const node = nodes[id];
  if (!node) return;
  selectedNode = id;
  document.querySelectorAll(".graph-node").forEach((element) => element.classList.toggle("selected", element.dataset.node === id));
  drawerTitle.textContent = node.title;
  drawerDetails.innerHTML = [
    ["Node kind", node.kind], ["Observed state", node.state], ["Recorded output digest", node.digest], ["Output", node.output]
  ].map(([term, value]) => `<div><dt>${term}</dt><dd>${value}</dd></div>`).join("");
  drawerCommand.textContent = node.command;
  if (open) {
    priorFocus = document.activeElement;
    drawer.classList.add("open");
    document.querySelector(`[data-node="${id}"]`)?.setAttribute("aria-expanded", "true");
    document.querySelector(".drawer-close").focus();
  }
}

function closeDrawer() {
  drawer.classList.remove("open");
  document.querySelectorAll(".graph-node[aria-expanded='true']").forEach((node) => node.setAttribute("aria-expanded", "false"));
  if (priorFocus instanceof HTMLElement) priorFocus.focus();
}

async function runDemo() {
  if (running) return;
  running = true;
  runButton.disabled = true;
  runButton.querySelector(".run-label").textContent = "Replaying";
  surface.dataset.state = "running";
  surface.dataset.stage = "0";
  document.querySelectorAll(".graph-node").forEach((node) => node.classList.remove("complete", "active"));
  for (const [index, stage] of stages.entries()) {
    surface.dataset.stage = String(index + 1);
    buildStatus.textContent = stage.label;
    logStep.textContent = `$ step ${String(index + 1).padStart(2, "0")} / ${stages.length}`;
    logMessage.textContent = stage.message;
    stage.ids.forEach((id) => document.querySelector(`[data-node="${id}"]`).classList.add("active"));
    await wait(index === 0 ? 520 : 760);
    stage.ids.forEach((id) => {
      const node = document.querySelector(`[data-node="${id}"]`);
      node.classList.remove("active"); node.classList.add("complete");
    });
  }
  surface.dataset.state = "done";
  buildStatus.textContent = "recorded by fixture runner";
  logStep.textContent = "$ replay complete · stored fixture evidence";
  logMessage.textContent = "7 nodes recorded; 3 fixture observations. No model runtime or llama.cpp claim inferred.";
  runButton.querySelector(".run-label").textContent = "Replay again";
  runButton.disabled = false; running = false;
}

document.querySelectorAll(".graph-node").forEach((node) => {
  node.setAttribute("aria-controls", "node-drawer");
  node.setAttribute("aria-expanded", "false");
  node.addEventListener("click", () => selectNode(node.dataset.node, true));
});
document.querySelector("#inspect-node").addEventListener("click", () => selectNode(selectedNode, true));
document.querySelector(".drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && drawer.classList.contains("open")) closeDrawer();
});
runButton.addEventListener("click", runDemo);

const toast = document.querySelector("#toast");
let toastTimer;
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const value = button.dataset.copy;
    try { await navigator.clipboard.writeText(value); toast.textContent = "Copied to clipboard"; }
    catch { toast.textContent = value; }
    toast.classList.add("show"); window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("show"), 1800);
  });
});

const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) { entry.target.classList.add("visible"); revealObserver.unobserve(entry.target); }
  });
}, { threshold: 0.12 });
document.querySelectorAll(".reveal").forEach((element) => revealObserver.observe(element));

function updateProgress() {
  const available = document.documentElement.scrollHeight - window.innerHeight;
  const ratio = available > 0 ? window.scrollY / available : 0;
  document.querySelector("#progress-bar").style.width = `${Math.min(100, Math.max(0, ratio * 100))}%`;
}
window.addEventListener("scroll", updateProgress, { passive: true });
updateProgress(); selectNode(selectedNode);

async function loadEvidence() {
  try {
    const response = await fetch("./demo-evidence.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const evidence = await response.json();
    if (evidence.schema !== "modelbake.site-evidence.v1" || evidence.nodes?.length !== 7) throw new Error("unexpected evidence schema");
    evidence.nodes.forEach((record) => {
      if (nodes[record.id]) nodes[record.id].digest = record.digest;
    });
    document.querySelector("#demo-source").textContent = `${evidence.source_digest.slice(7, 11)}…${evidence.source_digest.slice(-4)}`;
    document.querySelector("#cold-run").textContent = `${evidence.run_id.slice(0, 8)}…`;
    document.querySelector("#warm-run").textContent = `${evidence.repeat_run.run_id.slice(0, 8)}…`;
    buildStatus.textContent = "stored acceptance evidence";
    logMessage.textContent = "A sanitized seven-node manifest is ready to inspect or replay.";
    runButton.disabled = false;
    runButton.querySelector(".run-label").textContent = "Replay build record";
    selectNode(selectedNode);
  } catch (error) {
    buildStatus.textContent = "evidence unavailable";
    logMessage.textContent = `Could not load the bundled manifest: ${error.message}`;
    runButton.querySelector(".run-label").textContent = "Replay unavailable";
  }
}
loadEvidence();
