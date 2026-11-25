(() => {
  "use strict";

  const storageKey = "transcriptor.theme";
  const root = document.documentElement;
  const system = matchMedia("(prefers-color-scheme: dark)");
  let preference;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") preference = saved;
  } catch {
    // The system preference still works when storage is unavailable.
  }

  function apply() {
    const dark = (preference ?? (system.matches ? "dark" : "light")) === "dark";
    root.dataset.theme = dark ? "dark" : "light";
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.setAttribute("aria-pressed", String(dark));
      const label = dark ? toggle.dataset.switchLight : toggle.dataset.switchDark;
      toggle.setAttribute("aria-label", label ?? (dark ? "Switch to light theme" : "Switch to dark theme"));
    }
  }

  apply();
  system.addEventListener("change", apply);
  document.addEventListener("languagechange", apply);
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      preference = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(storageKey, preference);
      } catch {
        // The current page keeps the selected theme.
      }
      apply();
    });
  });
})();
