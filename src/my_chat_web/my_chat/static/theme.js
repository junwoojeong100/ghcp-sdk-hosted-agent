(() => {
  "use strict";

  const storageKey = "my-chat-theme";
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function preferredTheme() {
    const stored = window.localStorage.getItem(storageKey);
    if (stored === "light" || stored === "dark") {
      return stored;
    }
    return media.matches ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    const button = document.getElementById("theme-toggle");
    const icon = document.getElementById("theme-icon");
    if (button) {
      const dark = theme === "dark";
      button.setAttribute("aria-pressed", String(dark));
      button.setAttribute(
        "aria-label",
        dark ? "라이트 모드로 전환" : "다크 모드로 전환",
      );
      button.title = dark ? "라이트 모드" : "다크 모드";
    }
    if (icon) {
      icon.textContent = theme === "dark" ? "☀" : "☾";
    }
  }

  applyTheme(preferredTheme());

  window.addEventListener("DOMContentLoaded", () => {
    applyTheme(preferredTheme());
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      const next =
        document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      window.localStorage.setItem(storageKey, next);
      applyTheme(next);
    });
  });

  media.addEventListener("change", () => {
    if (!window.localStorage.getItem(storageKey)) {
      applyTheme(preferredTheme());
    }
  });
})();
