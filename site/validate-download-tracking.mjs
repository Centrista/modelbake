import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const siteRoot = join(dirname(fileURLToPath(import.meta.url)), "dist");
const documents = ["index.html", "real-evidence.html"];
const expected = [
  "evidence-json|evidence-header|0.1.0",
  "source|integration|0.1.0",
  "source|open|0.1.0",
  "source|workflow|0.1.0",
  "wheel|hero|0.1.0",
  "wheel|open|0.1.0"
];
const observed = [];

for (const name of documents) {
  const html = readFileSync(join(siteRoot, name), "utf8");
  assert.match(html, /<script src="\.\/download-tracking\.js"><\/script>/);
  const anchors = html.match(/<a\b[^>]*>/g) || [];
  for (const anchor of anchors) {
    const packageLink = /href="\.\/downloads\//.test(anchor);
    const tracked = /data-download-artifact=/.test(anchor);
    if (packageLink) assert.equal(tracked, true, `${name} has an untracked package link`);
    if (!tracked) continue;

    assert.match(anchor, /\sdownload(?:\s|>)/);
    const artifact = anchor.match(/data-download-artifact="([^"]+)"/)?.[1];
    const location = anchor.match(/data-download-location="([^"]+)"/)?.[1];
    const version = anchor.match(/data-download-version="([^"]+)"/)?.[1];
    observed.push(`${artifact}|${location}|${version}`);
  }
}

assert.deepEqual(observed.sort(), expected);

const tracker = readFileSync(join(siteRoot, "download-tracking.js"), "utf8");
assert.match(tracker, /window\.va\("pageview"/);
assert.match(tracker, /route: "\/download\/\[artifact\]\/\[version\]\/\[location\]"/);
assert.match(tracker, /productionHosts = new Set\(\["modelbake\.vercel\.app"\]\)/);
assert.doesNotMatch(tracker, /localStorage|sessionStorage|document\.cookie|link\.href/);

console.log(`download tracking contract passed (${observed.length} CTAs)`);
