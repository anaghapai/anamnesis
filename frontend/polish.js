/* Anamnesis polish layer. Load LAST (after app.js and the feature files). Adds, without editing them:
   toasts and a loading bar, a notification bell, a mobile menu, keyboard shortcuts, basic accessibility labels,
   a "My Questions" screen, a local-model status line, and a "share access directly" card on People. */
(function () {
  const $ = id => document.getElementById(id);
  const E = s => esc(s == null ? "" : String(s));
  const SVG = {
    bell: '<svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
    menu: '<svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg>'
  };

  // ---------------------------------------------------------------- toasts ---
  const tbox = document.createElement("div");
  tbox.id = "pl-toasts"; tbox.setAttribute("role", "status"); tbox.setAttribute("aria-live", "polite");
  document.body.appendChild(tbox);
  window.toast = function (text, kind) {
    const t = document.createElement("div");
    t.className = "pl-toast " + (kind || "");
    t.textContent = text;
    tbox.appendChild(t);
    while (tbox.children.length > 3) tbox.firstChild.remove();
    setTimeout(() => t.remove(), kind === "error" ? 6000 : 2800);
  };
  window.alert = m => { const s = String(m); toast(s, /fail|error|denied|cannot|can't|not |expired|invalid|unable|only /i.test(s) ? "error" : ""); };

  // --------------------------------------------- loading bar + save feedback ---
  const bar = document.createElement("div"); bar.id = "pl-bar"; document.body.appendChild(bar);
  let pending = 0, barTimer = null;
  const QUIET = /^\/(ask|help|search|auth|updates|my-work|llm)|explain|preview|what-if|simulate|audit\/verify/i;
  const origApi = window.api;
  window.api = async function (path, options = {}) {
    pending++; bar.classList.add("on"); bar.style.width = "70%"; clearTimeout(barTimer);
    const method = (options.method || "GET").toUpperCase();
    try {
      const r = await origApi(path, options);
      if (method !== "GET" && !QUIET.test(String(path))) toast("Saved", "ok");
      return r;
    } catch (e) {
      if (e instanceof TypeError) toast("Cannot reach the server. Is it still running?", "error");
      throw e;
    } finally {
      if (--pending <= 0) { pending = 0; bar.style.width = "100%"; barTimer = setTimeout(() => { bar.classList.remove("on"); bar.style.width = "0"; }, 250); }
    }
  };

  // -------------------------------------------------------- mobile menu ---
  const menu = document.createElement("button");
  menu.className = "pl-menu"; menu.innerHTML = SVG.menu; menu.setAttribute("aria-label", "Open menu");
  const scrim = document.createElement("div"); scrim.className = "pl-scrim";
  const appEl = $("app");
  appEl.appendChild(menu); appEl.appendChild(scrim);
  const closeMenu = () => document.body.classList.remove("menu-open");
  menu.addEventListener("click", () => document.body.classList.toggle("menu-open"));
  scrim.addEventListener("click", closeMenu);
  document.querySelector(".sidebar nav").addEventListener("click", e => { if (e.target.closest(".nav-item")) closeMenu(); });

  // ------------------------------------------------------ notification bell ---
  const bell = document.createElement("button");
  bell.className = "pl-bell"; bell.setAttribute("aria-label", "Notifications"); bell.setAttribute("aria-haspopup", "true");
  bell.innerHTML = SVG.bell + '<span class="pl-label">Inbox</span><span class="pl-count zero" id="pl-count">0</span>';
  const panel = document.createElement("div");
  panel.className = "pl-panel hidden"; panel.setAttribute("role", "region"); panel.setAttribute("aria-label", "Notifications");
  appEl.appendChild(bell); appEl.appendChild(panel);

  async function refreshBell(full) {
    if (!TOKEN || !ME) return;
    try {
      const [w, ar, inbox] = await Promise.all([
        origApi("/my-work"),
        atLeast("manager") ? origApi("/access-requests").catch(() => ({ for_review: [] })) : Promise.resolve({ for_review: [] }),
        full ? origApi("/updates") : Promise.resolve(null)
      ]);
      const reviews = atLeast("manager") ? w.pending_reviews : 0, access = ar.for_review.length;
      const total = (w.unread_updates || 0) + reviews + access;
      const c = $("pl-count"); c.textContent = total > 99 ? "99+" : total; c.classList.toggle("zero", !total);
      bell.setAttribute("aria-label", total ? `Notifications, ${total} need attention` : "Notifications");
      if (full) {
        const link = (n, text, view) => n ? `<button class="pl-link" data-go="${view}">${n} ${text}</button>` : "";
        panel.innerHTML = `<h4>Notifications</h4>` +
          link(reviews, reviews === 1 ? "answer waiting for your review" : "answers waiting for your review", "reviews") +
          link(access, access === 1 ? "access request to decide" : "access requests to decide", "documents") +
          (inbox.length ? inbox.slice(0, 6).map(u => `<div class="pl-item ${u.read ? "" : "unread"}"><small>${E(u.from)} &middot; ${E(fmt(u.created_at))}</small>${E(u.text)}</div>`).join("")
            : `<div class="pl-item">Nothing new.</div>`) +
          `<div class="pl-foot"><button class="btn-ghost" data-go="mywork">Open My Work</button>` +
          (w.unread_updates ? `<button class="btn-ghost" id="pl-readall">Mark all read</button>` : "") + `</div>`;
      }
    } catch { /* offline or logged out: keep the last count */ }
  }
  bell.addEventListener("click", () => {
    const open = panel.classList.toggle("hidden") === false;
    bell.setAttribute("aria-expanded", String(open));
    if (open) refreshBell(true);
  });
  panel.addEventListener("click", async e => {
    const go = e.target.closest("[data-go]");
    if (go) { panel.classList.add("hidden"); showView(go.dataset.go); return; }
    if (e.target.id === "pl-readall") { await origApi("/updates/read-all", { method: "POST" }); refreshBell(true); if (window.refreshPills) refreshPills(); }
  });
  document.addEventListener("click", e => { if (!panel.classList.contains("hidden") && !e.target.closest(".pl-panel,.pl-bell")) panel.classList.add("hidden"); });
  setInterval(() => { if (!appEl.classList.contains("hidden")) refreshBell(false); }, 45000);

  // ------------------------------------------------------ new nav items ---
  const sidebarNav = document.querySelector(".sidebar nav");
  const afterSearch = sidebarNav.querySelector('[data-view="search"]') || sidebarNav.querySelector('[data-view="ask"]');
  afterSearch.insertAdjacentHTML("afterend", '<button class="nav-item" data-view="myqs">My Questions</button>');
  sidebarNav.querySelector('[data-view="myqs"]').addEventListener("click", () => showView("myqs"));

  // ----------------------------------------------------- My Questions view ---
  $("view-health").parentNode.insertAdjacentHTML("beforeend", `
    <section id="view-myqs" class="view"><h2>My Questions</h2>
      <p class="view-sub">Your last 25 questions, what happened to them, and any correction from a reviewer.</p>
      <div id="myqs-list"></div></section>`);
  const STATE = { verified: "Verified", corrected: "Corrected by a reviewer", flagged: "Flagged, waiting for review", wrong: "Marked wrong", answered: "Answered", open: "Answered" };
  async function loadMyQs() {
    const box = $("myqs-list");
    box.innerHTML = `<div class="empty-note">Loading...</div>`;
    let rows;
    try { rows = await origApi("/qa/mine"); } catch (e) { box.innerHTML = `<div class="empty-note">Could not load: ${E(e.message)}</div>`; return; }
    box.innerHTML = rows.length ? rows.map(r => `
      <div class="card pl-q">
        <h4>${E(r.question)}</h4>
        <div><span class="badge badge-internal">${E(STATE[r.status] || r.status)}</span> <span class="muted">${E(fmt(r.created_at))}</span></div>
        <p>${E((r.answer || "No answer was found.").slice(0, 280))}${(r.answer || "").length > 280 ? "..." : ""}</p>
        ${r.flag_note ? `<div class="pl-note"><b>Your flag:</b> ${E(r.flag_note)}</div>` : ""}
        ${r.corrected_answer ? `<div class="pl-note"><b>Corrected answer${r.reviewer ? " by " + E(r.reviewer) : ""}:</b> ${E(r.corrected_answer)}</div>` : ""}
        ${r.review_note ? `<div class="pl-note"><b>Reviewer note:</b> ${E(r.review_note)}</div>` : ""}
        <button class="btn-ghost" data-ask-again="${E(r.question)}">Ask again</button>
      </div>`).join("") : `<div class="empty-note">You have not asked anything yet. Open Ask to start.</div>`;
  }
  $("myqs-list").addEventListener("click", e => {
    const b = e.target.closest("[data-ask-again]"); if (!b) return;
    showView("ask"); const i = $("ask-input"); i.value = b.dataset.askAgain; i.focus();
  });
  window.FEATURE_VIEWS = Object.assign(window.FEATURE_VIEWS || {}, { myqs: loadMyQs });

  // ------------------------------------------- local model status in sidebar ---
  const logout = $("logout-btn");
  logout.insertAdjacentHTML("beforebegin", '<div class="pl-llm" id="pl-llm"><span class="pl-dot"></span><span>Local model: checking</span></div>');
  async function refreshLlm() {
    try {
      const s = await origApi("/llm/status");
      $("pl-llm").innerHTML = `<span class="pl-dot ${s.available ? "on" : ""}"></span><span>${s.available ? "Local model on: " + E(s.model) : "Local model off (answers stay extractive)"}</span>`;
      $("pl-llm").title = s.available ? "Runs on this machine only. Nothing leaves it." : (s.hint || "");
    } catch { $("pl-llm").innerHTML = `<span class="pl-dot"></span><span>Local model: unavailable</span>`; }
  }

  // ------------------------------------- share access directly (People view) ---
  const peopleView = $("view-people");
  peopleView.querySelector("h2").parentNode.insertAdjacentHTML("afterend", `
    <div id="pl-grant" class="card hidden" style="padding:14px">
      <strong>Share access directly</strong>
      <div class="muted" style="margin:4px 0 10px">Give someone temporary access to a department's documents without waiting for a request. It expires automatically and is written to the audit log.</div>
      <div class="row-gap">
        <select id="pl-g-user" aria-label="Person"></select>
        <select id="pl-g-dept" aria-label="Department"></select>
        <input type="number" id="pl-g-hours" value="24" min="1" max="720" style="max-width:110px" aria-label="Hours">
        <button class="btn-primary" id="pl-g-go">Share</button>
      </div></div>`);
  async function loadGrant() {
    const card = $("pl-grant");
    if (!atLeast("manager")) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    try {
      const people = (await origApi("/org/employees")).filter(p => p.id !== ME.id && p.active !== false);
      fillSelect($("pl-g-user"), people.map(p => ({ value: p.id, label: `${p.name} (${p.role}, ${p.department})` })));
      fillSelect($("pl-g-dept"), ME.role === "manager" ? [ME.department] : ME.departments.filter(d => d !== "All"));
    } catch { card.classList.add("hidden"); }
  }
  $("pl-g-go").addEventListener("click", async () => {
    try {
      await origApi("/access-requests/grant", { method: "POST", body: JSON.stringify({
        user_id: Number($("pl-g-user").value), department: $("pl-g-dept").value, duration_hours: Number($("pl-g-hours").value) || 24 }) });
      toast("Access shared. The person was notified.", "ok");
    } catch (e) { toast(e.message, "error"); }
  });

  // --------------------------------------------- hook into view switching ---
  const origShow = window.showView;
  window.showView = function (name) { origShow(name); if (name === "people") loadGrant(); closeMenu(); };

  // --------------------------------------------------------- keyboard shortcuts ---
  const keys = document.createElement("div");
  keys.id = "pl-keys"; keys.className = "hidden"; keys.setAttribute("role", "dialog"); keys.setAttribute("aria-label", "Keyboard shortcuts");
  keys.innerHTML = `<div class="box"><h3 style="margin-top:0">Keyboard shortcuts</h3>
    <div><span>Focus the question box</span><span class="pl-kbd">/</span></div>
    <div><span>Go to Dashboard / Ask / Search / My Work</span><span><span class="pl-kbd">g</span> then <span class="pl-kbd">d</span> <span class="pl-kbd">a</span> <span class="pl-kbd">s</span> <span class="pl-kbd">w</span></span></div>
    <div><span>Show this list</span><span class="pl-kbd">?</span></div>
    <div><span>Close panels</span><span class="pl-kbd">Esc</span></div></div>`;
  document.body.appendChild(keys);
  keys.addEventListener("click", () => keys.classList.add("hidden"));
  let gWait = 0;
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { keys.classList.add("hidden"); panel.classList.add("hidden"); closeMenu(); return; }
    if (appEl.classList.contains("hidden") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(input|textarea|select)$/i.test(e.target.tagName) || e.target.isContentEditable) return;
    if (e.key === "?") { keys.classList.toggle("hidden"); return; }
    if (e.key === "/") {
      e.preventDefault();
      if (document.querySelector("#view-search.active")) $("sr-q").focus();
      else { if (!document.querySelector("#view-ask.active")) showView("ask"); $("ask-input").focus(); }
      return;
    }
    if (e.key === "g") { gWait = Date.now(); return; }
    if (Date.now() - gWait < 1200) {
      const to = { d: "dashboard", a: "ask", s: "search", w: "mywork" }[e.key];
      if (to) { gWait = 0; showView(to); }
    }
  });

  // ------------------------------------------------------------ accessibility ---
  function a11y() {
    document.querySelector(".sidebar nav").setAttribute("aria-label", "Main");
    document.querySelector(".content").setAttribute("role", "main");
    document.querySelectorAll(".nav-item").forEach(b => b.classList.contains("active") ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
    document.querySelectorAll("input:not([aria-label]),select:not([aria-label]),textarea:not([aria-label])").forEach(el => {
      if (el.type === "hidden" || el.type === "checkbox" || el.type === "radio" || el.id && document.querySelector(`label[for="${el.id}"]`)) return;
      const t = el.getAttribute("placeholder") || el.getAttribute("title"); if (t) el.setAttribute("aria-label", t);
    });
    document.querySelectorAll("button:not([aria-label])").forEach(b => { if (!b.textContent.trim() && b.title) b.setAttribute("aria-label", b.title); });
  }
  let q = false;
  new MutationObserver(() => { if (q) return; q = true; requestAnimationFrame(() => { q = false; a11y(); }); })
    .observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
  a11y();

  // -------------------------------------------------------------- after login ---
  const prevBoot = window.afterBoot;
  window.afterBoot = function (f) { if (prevBoot) prevBoot(f); refreshBell(false); refreshLlm(); };
  if (typeof ME !== "undefined" && ME && !appEl.classList.contains("hidden")) { refreshBell(false); refreshLlm(); }
})();
