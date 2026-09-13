import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const siteRoot = join(dirname(fileURLToPath(import.meta.url)), "dist");
const documents = ["index.html", "real-evidence.html", "git-vs-modelbake.html"];
const expected = [
  "evidence-json|evidence-header|0.1.0",
  "source|integration|0.1.0",
  "source|open|0.1.0",
  "source|workflow|0.1.0",
  "wheel|git-guide|0.1.0",
  "wheel|hero|0.1.0",
  "wheel|open|0.1.0"
];
const observed = [];

for (const name of documents) {
  const html = readFileSync(join(siteRoot, name), "utf8");
  assert.match(html, /<script src="\.\/download-tracking\.js"><\/script>/);
  const anchors = html.match(/<a\b[^>]*>/g) || [];
  for (const anchor of anchors) {
    const packageLink = /href="\.\/downloads\/[^"]+\.(?:whl|tar\.gz)"/.test(anchor);
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
assert.match(tracker, /route: "\/intent\/install\/\[version\]\/\[location\]"/);
assert.match(tracker, /"git-guide"/);
assert.match(
  tracker,
  /productionHosts = new Set\(\["modelbake\.dev", "www\.modelbake\.dev", "modelbake\.vercel\.app"\]\)/
);
assert.doesNotMatch(tracker, /localStorage|sessionStorage|document\.cookie|link\.href/);

const index = readFileSync(join(siteRoot, "index.html"), "utf8");
assert.match(index, /<meta name="robots" content="index, follow, max-image-preview:large" \/>/);
assert.match(index, /<script type="application\/ld\+json">/);
assert.match(index, /"@type": "SoftwareApplication"/);
assert.match(index, /"price": "0"/);
assert.match(index, /href="https:\/\/modelbake\.dev\/llms\.txt"/);
assert.match(index, /data-install-intent="hero" data-install-version="0\.1\.0"/);
assert.match(index, /data-install-intent="open" data-install-version="0\.1\.0"/);
assert.match(index, /python -m pip install https:\/\/modelbake\.dev\/downloads\/modelbake_ai-0\.1\.0-py3-none-any\.whl/);

for (const name of ["robots.txt", "sitemap.xml", "llms.txt", "downloads/SHA256SUMS"]) {
  assert.equal(existsSync(join(siteRoot, name)), true, `${name} is missing`);
}

const robots = readFileSync(join(siteRoot, "robots.txt"), "utf8");
assert.match(robots, /Sitemap: https:\/\/modelbake\.dev\/sitemap\.xml/);

const sitemap = readFileSync(join(siteRoot, "sitemap.xml"), "utf8");
assert.match(sitemap, /<loc>https:\/\/modelbake\.dev\/<\/loc>/);
assert.match(sitemap, /<loc>https:\/\/modelbake\.dev\/real-evidence\.html<\/loc>/);
assert.match(sitemap, /<loc>https:\/\/modelbake\.dev\/git-vs-modelbake\.html<\/loc>/);

const gitGuide = readFileSync(join(siteRoot, "git-vs-modelbake.html"), "utf8");
assert.match(gitGuide, /Git tracks the code/);
assert.match(gitGuide, /ModelBake produces a readable manifest/);
assert.match(gitGuide, /It records evidence\. It does not certify model quality/);

const llms = readFileSync(join(siteRoot, "llms.txt"), "utf8");
assert.match(llms, /ModelBake is a free, Apache-2\.0 local CLI/);
assert.match(llms, /modelbake tour/);

const sums = readFileSync(join(siteRoot, "downloads", "SHA256SUMS"), "utf8");
assert.match(sums, /^acfc001f07001064054aaaff14637748074fe033b3fd5f2a337c027bdd10f320  modelbake_ai-0\.1\.0-py3-none-any\.whl$/m);
assert.match(sums, /^f63c7fce794d79629776e7a0a7d2c12945a5dd30f76b97f27b60f2ce1ee4dfe2  modelbake_ai-0\.1\.0\.tar\.gz$/m);

console.log(`download tracking contract passed (${observed.length} CTAs)`);
