"use strict";

(() => {
  const key = "orca-theme";
  const root = document.documentElement;
  try {
    root.dataset.theme = localStorage.getItem(key) === "dark" ? "dark" : "light";
  } catch {
    root.dataset.theme = "light";
  }
  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("theme-toggle");
    const update = () => toggle.setAttribute("aria-pressed", String(root.dataset.theme === "dark"));
    update();
    toggle.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(key, root.dataset.theme);
      } catch {
        // The current theme remains usable when the browser disallows storage.
      }
      update();
    });
    window.addEventListener("storage", (event) => {
      if (event.key !== key && event.key !== null) return;
      root.dataset.theme = event.newValue === "dark" ? "dark" : "light";
      update();
    });
  });
})();
