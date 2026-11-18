"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const trigger = $("account-trigger");
  const menu = $("account-menu");
  if (trigger && menu) {
    const setOpen = open => {
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", String(open));
    };
    trigger.addEventListener("click", event => {
      event.stopPropagation();
      setOpen(menu.hidden);
    });
    menu.addEventListener("click", event => {
      if (event.target.closest("[role='menuitem']")) setOpen(false);
    });
    document.addEventListener("click", event => {
      if (!menu.hidden && !trigger.contains(event.target) && !menu.contains(event.target)) setOpen(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !menu.hidden) { setOpen(false); trigger.focus(); }
    });
  }
  const railButton = $("collapse-sidebar");
  if (railButton && !railButton.dataset.railBound) {
    railButton.dataset.railBound = "1";
    const applyRail = on => {
      document.querySelector(".app-frame")?.classList.toggle("is-rail", on);
      document.querySelector(".verilo-sidebar")?.classList.toggle("collapsed", on);
      $("shell")?.classList.toggle("rail", on);
      railButton.setAttribute("aria-pressed", String(on));
    };
    try { if (localStorage.getItem("verilo.sidebar.collapsed") === "1") applyRail(true); } catch { /* keep the expanded sidebar */ }
    railButton.addEventListener("click", () => {
      const on = !railButton.getAttribute("aria-pressed") || railButton.getAttribute("aria-pressed") === "false";
      applyRail(on);
      try { localStorage.setItem("verilo.sidebar.collapsed", on ? "1" : "0"); } catch { /* the layout still toggles */ }
    });
  }
  if (trigger) trigger.dataset.accountBound = "1";
  const usd = micro => "$" + (Number(micro || 0) / 1e6).toFixed(4);
  async function read(path) {
    const response = await fetch(path, {headers: {Accept: "application/json", "X-Loom-Console": "local"}, credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }
  async function fillAccount() {
    try {
      const me = await read("/v1/me");
      const login = String(me.display_name || me.subject_id || "").trim();
      if ($("account-name")) $("account-name").textContent = login;
      const initial = Array.from(login)[0] || "";
      if ($("account-avatar")) $("account-avatar").textContent = /[a-z]/i.test(initial) ? initial.toUpperCase() : initial;
      if (trigger && login) trigger.setAttribute("aria-label", "账号菜单，" + login);
      const hide = me.authentication !== "public-session";
      if ($("account-signout")) $("account-signout").hidden = hide;
      if ($("account-signout-sep")) $("account-signout-sep").hidden = hide;
    } catch { /* the page still navigates without a name */ }
    const row = $("account-usage");
    if (!row) return;
    try {
      const settings = await read("/v1/assistant/settings");
      const budget = Number(settings.daily_budget_microusd || 0);
      const spent = Number(settings.spent_today_microusd || 0);
      if (!budget && !spent) return;
      const pct = budget > 0 ? Math.round(spent / budget * 100) : null;
      $("account-usage-meta").textContent = pct === null ? usd(spent) : pct + "% 已用";
      const fill = $("account-usage-fill");
      if (fill) fill.style.width = Math.min(100, pct || 0) + "%";
      row.hidden = false;
      if ($("account-usage-sep")) $("account-usage-sep").hidden = false;
    } catch { row.hidden = true; }
  }
  $("account-signout")?.addEventListener("click", async () => {
    try { await fetch("/preview/logout", {method: "POST", credentials: "same-origin"}); }
    finally { location.assign("/preview/login"); }
  });
  fillAccount();
})();
