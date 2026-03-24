(function () {
  const KEY = "crm_sidebar_collapsed";
  const mq = window.matchMedia("(min-width: 769px)");

  function syncToggle() {
    const btn = document.getElementById("sidebar-toggle");
    if (!btn) return;
    const collapsed =
      mq.matches && document.documentElement.classList.contains("sidebar-collapsed");
    btn.textContent = collapsed ? "›" : "☰";
    btn.setAttribute("aria-label", collapsed ? "Развернуть меню" : "Свернуть меню");
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }

  function onResize() {
    if (!mq.matches) {
      document.documentElement.classList.remove("sidebar-collapsed");
    }
    syncToggle();
  }

  document.getElementById("sidebar-toggle")?.addEventListener("click", function () {
    if (!mq.matches) return;
    const root = document.documentElement;
    const next = !root.classList.contains("sidebar-collapsed");
    root.classList.toggle("sidebar-collapsed", next);
    try {
      localStorage.setItem(KEY, next ? "1" : "");
    } catch (e) {
      /* ignore */
    }
    syncToggle();
  });

  mq.addEventListener("change", onResize);
  syncToggle();
})();
