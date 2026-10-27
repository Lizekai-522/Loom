"use strict";
// The session is HttpOnly; no provider key or API bearer enters the page.
window.LoomPreview = {
  active: window.location.protocol === "https:",
  configure(principal, newExample) {
    this.active = principal.authentication === "public-session";
    if (!this.active) return;
    document.querySelector(".environment").textContent = "临时公网体验 · 共享测试空间";
    document.getElementById("logout-button").textContent = "退出体验";
    document.title = "Loom · 公网体验工作台";
    const cost = document.getElementById("goal-cost-limit");
    const tokens = document.getElementById("goal-token-limit");
    if (cost) { cost.value = "1000000"; cost.max = "1000000"; }
    if (tokens) { tokens.value = "3200000"; tokens.max = "3200000"; }
    if (!document.getElementById("preview-new-example")) {
      const button = document.createElement("button"); button.id = "preview-new-example";
      button.type = "button"; button.textContent = "新建词频体验";
      button.addEventListener("click", newExample);
      document.querySelector(".sidebar .section-heading").after(button);
    }
  },
  async logout() {
    if (!this.active) return false;
    const response = await fetch("/preview/logout", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    if (response.ok || response.status === 401) window.location.assign("/preview/login");
    else throw new Error("PUBLIC_LOGOUT_FAILED");
    return true;
  }
};
