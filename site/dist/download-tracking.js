(() => {
  const productionHosts = new Set(["modelbake.dev", "www.modelbake.dev", "modelbake.vercel.app"]);
  const allowedArtifacts = new Set(["wheel", "source", "evidence-json"]);
  const allowedLocations = new Set([
    "hero",
    "integration",
    "workflow",
    "open",
    "git-guide",
    "evidence-header"
  ]);
  const allowedIntentLocations = new Set(["hero", "open"]);
  const versionPattern = /^\d+\.\d+\.\d+$/;

  window.va = window.va || function (...parameters) {
    window.vaq = window.vaq || [];
    window.vaq.push(parameters);
  };

  if (productionHosts.has(window.location.hostname)) {
    const script = document.createElement("script");
    script.defer = true;
    script.src = "/_vercel/insights/script.js";
    document.head.appendChild(script);
  }

  document.querySelectorAll("[data-download-artifact]").forEach((link) => {
    link.addEventListener("click", () => {
      const artifact = link.dataset.downloadArtifact;
      const location = link.dataset.downloadLocation;
      const version = link.dataset.downloadVersion;
      if (!allowedArtifacts.has(artifact) || !allowedLocations.has(location) || !versionPattern.test(version)) return;

      window.va("pageview", {
        route: "/download/[artifact]/[version]/[location]",
        path: `/download/${artifact}/${version}/${location}`
      });
    });
  });

  document.querySelectorAll("[data-install-intent]").forEach((button) => {
    button.addEventListener("click", () => {
      const location = button.dataset.installIntent;
      const version = button.dataset.installVersion;
      if (!allowedIntentLocations.has(location) || !versionPattern.test(version)) return;

      window.va("pageview", {
        route: "/intent/install/[version]/[location]",
        path: `/intent/install/${version}/${location}`
      });
    });
  });
})();
