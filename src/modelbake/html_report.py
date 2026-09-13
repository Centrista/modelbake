"""Self-contained, non-executable HTML evidence reports for ModelBake runs."""

from __future__ import annotations

import html
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _text(value: Any, fallback: str = "unknown") -> str:
    rendered = fallback if value is None else str(value)
    return html.escape(rendered, quote=True)


def _node_markup(node: Mapping[str, Any], position: int) -> str:
    state = str(node.get("state", "unknown"))
    state_class = {
        "produced": "produced",
        "fixture_observed": "fixture",
        "executed_on_runner": "executed",
        "cache_hit": "cached",
        "failed": "failed",
        "blocked": "blocked",
    }.get(state, "neutral")
    outputs = node.get("output_digests", {})
    digest = "—"
    if isinstance(outputs, Mapping) and outputs:
        digest = str(next(iter(outputs.values())))
    error = node.get("error")
    error_markup = f'<p class="error">{_text(error)}</p>' if error else ""
    return f"""
      <article class="node">
        <span class="number">{position:02d}</span>
        <div class="node-main">
          <small>{_text(node.get("kind"), "node")}</small>
          <h3>{_text(node.get("id"), "unnamed")}</h3>
          <code>{_text(digest)}</code>
          {error_markup}
        </div>
        <span class="state {state_class}">{_text(state)}</span>
      </article>"""


def render_html_report(data: Mapping[str, Any]) -> str:
    """Render observations without adding compatibility or quality conclusions."""

    nodes_value = data.get("nodes", [])
    nodes = [node for node in nodes_value if isinstance(node, Mapping)] if isinstance(nodes_value, list) else []
    runner_value = data.get("runner", {})
    runner = runner_value if isinstance(runner_value, Mapping) else {}
    observed_value = runner.get("observed", runner)
    observed = observed_value if isinstance(observed_value, Mapping) else {}
    runner_label = " · ".join(
        value
        for value in (
            str(observed.get("os", "")),
            str(observed.get("machine", "")),
            str(observed.get("python_version", "")),
        )
        if value
    ) or "recorded in manifest"
    exclusions_value = data.get("exclusions", [])
    exclusions = exclusions_value if isinstance(exclusions_value, list) else []
    exclusion_markup = "".join(f"<li>{_text(item)}</li>" for item in exclusions)
    node_markup = "".join(_node_markup(node, index) for index, node in enumerate(nodes, 1))
    status = str(data.get("status", "unknown"))
    status_class = "success" if status == "succeeded" else "failure"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>ModelBake · {_text(data.get("project"), "run")}</title>
  <style>
    :root{{--ink:#0e0e0e;--panel:#171717;--line:#383838;--paper:#fffefa;--muted:#9a9a94;--hot:#ff5c35;--acid:#d8ff52;--red:#ff765f;--mono:"SFMono-Regular",Consolas,monospace;--sans:"Helvetica Neue",Arial,sans-serif}}
    *{{box-sizing:border-box}}body{{margin:0;color:var(--paper);background:var(--ink);font-family:var(--sans);line-height:1.45}}main{{width:min(1120px,calc(100% - 2rem));margin:auto;padding:2rem 0 6rem}}header{{display:grid;grid-template-columns:1fr auto;gap:2rem;align-items:end;padding:4rem 0 2rem;border-bottom:2px solid var(--paper)}}.brand{{font-weight:800;letter-spacing:-.04em}}.brand i{{display:inline-grid;place-items:center;width:2rem;height:2rem;margin-right:.6rem;color:var(--hot);background:var(--paper);border-radius:4px;font-family:var(--mono);font-style:normal;font-size:.8rem}}.scope{{color:var(--muted);font-family:var(--mono);font-size:.65rem;text-align:right}}h1{{max-width:850px;margin:3.5rem 0 1rem;font-size:clamp(3.6rem,9vw,8rem);line-height:.8;letter-spacing:-.08em;text-transform:uppercase}}.deck{{max-width:50rem;color:var(--muted);font-size:1.05rem}}.summary{{display:grid;grid-template-columns:repeat(4,1fr);margin:3rem 0;border:1px solid var(--line)}}.summary div{{padding:1rem;border-right:1px solid var(--line);min-width:0}}.summary div:last-child{{border:0}}small{{display:block;color:#73736f;font-family:var(--mono);font-size:.56rem;letter-spacing:.1em;text-transform:uppercase}}.summary strong,.summary code{{display:block;margin-top:.4rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem}}.status{{color:var(--acid)}}.status.failure{{color:var(--red)}}.section-label{{margin:4rem 0 1rem;color:var(--hot);font-family:var(--mono);font-size:.63rem;letter-spacing:.12em}}.nodes{{border-top:1px solid var(--paper)}}.node{{display:grid;grid-template-columns:3rem 1fr auto;gap:1rem;align-items:center;min-height:7rem;padding:1rem 0;border-bottom:1px solid var(--line)}}.number{{align-self:start;color:#666;font-family:var(--mono);font-size:.58rem}}.node h3{{margin:.4rem 0;font-size:1.35rem;letter-spacing:-.045em}}.node code{{display:block;max-width:45rem;color:#777;font-size:.58rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.state{{padding:.35rem .5rem;border:1px solid var(--line);font-family:var(--mono);font-size:.58rem}}.state.produced{{color:var(--hot);border-color:var(--hot)}}.state.fixture{{color:#66d9ca;border-color:#3d8d83}}.state.executed{{color:var(--acid);border-color:var(--acid)}}.state.cached{{color:#cbb6ff;border-color:#745d9e}}.state.failed,.state.blocked{{color:var(--red);border-color:var(--red)}}.error{{margin:.5rem 0 0;color:var(--red);font-family:var(--mono);font-size:.62rem}}.boundary{{display:grid;grid-template-columns:.65fr 1.35fr;gap:3rem;margin-top:5rem;padding:2rem;border:1px solid var(--hot);background:#14110f}}.stamp{{display:grid;place-items:center;aspect-ratio:1;max-width:13rem;border:1px dashed var(--hot);border-radius:50%;color:var(--hot);font-family:var(--mono);font-size:.7rem;text-align:center;transform:rotate(-7deg)}}.boundary h2{{margin:0;font-size:clamp(2rem,4vw,4rem);line-height:.9;letter-spacing:-.06em;text-transform:uppercase}}.boundary p{{color:var(--muted)}}.boundary ul{{padding-left:1.2rem;color:#babaae;font-size:.82rem}}footer{{margin-top:4rem;color:#696965;font-family:var(--mono);font-size:.58rem}}@media(max-width:680px){{header,.boundary{{grid-template-columns:1fr}}.scope{{text-align:left}}.summary{{grid-template-columns:1fr 1fr}}.summary div:nth-child(2){{border-right:0}}.summary div:nth-child(-n+2){{border-bottom:1px solid var(--line)}}.node{{grid-template-columns:2rem 1fr}}.state{{grid-column:2;width:fit-content}}}}
  </style>
</head>
<body>
  <main>
    <header><div class="brand"><i>M</i>ModelBake</div><div class="scope">GENERATED EVIDENCE REPORT<br>CLAIMS APPLY ONLY TO RECORDED OBSERVATIONS</div></header>
    <h1>{_text(data.get("project"), "Unnamed run")}</h1>
    <p class="deck">This report names the bytes, steps, cache decisions, and runner observations recorded by one build. It deliberately makes no model-quality or broad compatibility claim.</p>
    <section class="summary" aria-label="Run summary">
      <div><small>Recorded status</small><strong class="status {status_class}">{_text(status)}</strong></div>
      <div><small>Run</small><code>{_text(data.get("run_id"))}</code></div>
      <div><small>Recipe</small><code>{_text(data.get("recipe_digest"))}</code></div>
      <div><small>Runner</small><code>{_text(runner_label)}</code></div>
    </section>
    <div class="section-label">01 / OBSERVED BUILD GRAPH</div>
    <section class="nodes">{node_markup}</section>
    <section class="boundary">
      <div class="stamp">OBSERVED<br>NOT INFERRED</div>
      <div><div class="section-label">02 / TRUTH BOUNDARY</div><h2>A green process exit is not a green model.</h2><p>States distinguish fixture-only observations from a successful external process under the recorded runner fingerprint.</p><ul>{exclusion_markup or '<li>No unrecorded assurance is implied.</li>'}</ul></div>
    </section>
    <footer>ModelBake · {_text(data.get("schema"))} · generated from manifest bytes</footer>
  </main>
</body>
</html>
"""


def write_html_report(data: Mapping[str, Any], destination: str | Path) -> Path:
    """Atomically write a self-contained report and return its resolved path."""

    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(render_html_report(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return target
