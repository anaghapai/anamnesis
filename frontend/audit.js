// Anamnesis Audit Log page: categories, search, filters, detail view. Talks to GET /audit/events (manager+ only,
// enforced on the server). Replaces the plain list through FEATURE_VIEWS.audit. The log is an append-only
// application audit log; each row is hash-chained to the one before it, and "Verify integrity" checks the chain.
(function () {
  const $ = id => document.getElementById(id);
  const E = s => esc(s == null ? "" : String(s));
  const PAGE = 50;
  let cat = "", offset = 0, seq = 0, timer = null, built = false, loaded = [];
  const COLORS = { "Authentication": "#22d3ee", "Access & permissions": "#f59e0b", "Document changes": "#60a5fa",
    "Rollbacks": "#f97316", "Knowledge changes": "#a78bfa", "Answer generation": "#34d399", "Verification": "#4ade80",
    "Escalation": "#f472b6", "Administration": "#94a3b8", "Other": "#64748b" };
  const badge = c => `<span class="au-badge" style="border-color:${COLORS[c] || "#64748b"};color:${COLORS[c] || "#64748b"}">${E(c)}</span>`;

  function build() {
    if (built) return;
    built = true;
    const v = $("view-audit");
    v.innerHTML = `
      <style>
        .au-chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
        .au-chip{background:transparent;border:1px solid var(--border);color:var(--text-dim);border-radius:999px;padding:5px 12px;cursor:pointer;font-size:12px}
        .au-chip.on{border-color:var(--cyan);color:var(--cyan);background:var(--bg-card)}
        .au-filters{display:grid;grid-template-columns:2fr repeat(3,minmax(120px,1fr)) auto;gap:8px;align-items:center}
        .au-badge{display:inline-block;border:1px solid;border-radius:999px;padding:1px 9px;font-size:11px;white-space:nowrap}
        .au-row{padding:10px 12px;margin-bottom:6px;cursor:pointer}
        .au-row:hover{border-color:var(--cyan)}
        .au-top{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
        .au-time{color:var(--text-dim);font-size:12px;min-width:150px}
        .au-detail-text{color:var(--text-dim);font-size:12px;margin-top:4px;word-break:break-word}
        .au-panel{margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:13px}
        .au-mini{font-size:12px;color:var(--text-dim);margin:2px 0}
        @media(max-width:900px){.au-filters{grid-template-columns:1fr 1fr}}
      </style>
      <h2>Audit Log</h2>
      <p class="view-sub">Who did what, and when. Visible to managers and above. Every row is hash-chained to the one before it, so an edited or removed row can be detected.</p>
      <div class="card" style="padding:14px">
        <div class="au-filters">
          <input type="text" id="au-q" placeholder="Search actor, action or details">
          <select id="au-actor"><option value="">Any person</option></select>
          <input type="date" id="au-from" title="From">
          <input type="date" id="au-to" title="To">
          <button class="btn-ghost" id="au-clear">Clear</button>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
          <button class="btn-secondary" id="au-verify-btn">Verify integrity</button>
          <input type="text" id="au-noted" placeholder="Head hash you noted earlier (optional)" aria-label="Head hash you noted earlier" style="min-width:240px;flex:1;max-width:360px" maxlength="64">
          <button class="btn-secondary" id="au-export">Export CSV</button>
        </div>
        <div id="au-verify" class="au-verify hidden" role="status"></div>
        <div class="au-chips" id="au-chips"></div>
      </div>
      <div id="au-count" class="muted" style="margin:12px 0 6px"></div>
      <div id="au-list"></div>
      <div style="margin:10px 0"><button class="btn-ghost hidden" id="au-more">Load more</button></div>`;
    const soon = () => { clearTimeout(timer); timer = setTimeout(() => load(true), 250); };
    $("au-q").addEventListener("input", soon);
    ["au-actor", "au-from", "au-to"].forEach(id => $(id).addEventListener("change", () => load(true)));
    $("au-clear").addEventListener("click", () => {
      ["au-q", "au-actor", "au-from", "au-to"].forEach(id => { $(id).value = ""; });
      cat = ""; load(true);
    });
    $("au-more").addEventListener("click", () => load(false));
    $("au-verify-btn").addEventListener("click", verify);
    $("au-export").addEventListener("click", exportCsv);
    $("au-chips").addEventListener("click", e => {
      const b = e.target.closest("[data-cat]"); if (!b) return;
      cat = b.dataset.cat; load(true);
    });
    $("au-list").addEventListener("click", e => {
      const row = e.target.closest("[data-au]"); if (!row) return;
      toggle(row);
    });
  }

  function params(off) {
    const p = new URLSearchParams();
    const add = (k, id) => { const v = $(id).value.trim(); if (v) p.set(k, v); };
    add("q", "au-q"); add("actor_id", "au-actor"); add("date_from", "au-from"); add("date_to", "au-to");
    if (cat) p.set("category", cat);
    p.set("tz_offset", new Date().getTimezoneOffset());      // so "today" means your today, not UTC's
    p.set("limit", PAGE); p.set("offset", off);
    return p.toString();
  }

  async function verify() {
    const box = $("au-verify");
    box.className = "au-verify"; box.textContent = "Checking every row...";
    try {
      const noted = ($("au-noted").value || "").trim();
      const v = await api("/audit/verify" + (noted ? "?noted_head=" + encodeURIComponent(noted) : ""));
      const old = v.unprotected_older_rows ? ` ${v.unprotected_older_rows} older row(s) were written before hash-chaining existed and cannot be checked.` : "";
      if (v.ok) {
        const a = v.anchor || {};
        let anchorMsg = "";
        if (a.status === "created") anchorMsg = " Head saved outside the database for the next check.";
        else if (a.status === "matches") anchorMsg = ` Matches the head saved on ${a.saved_at}: no rows were removed since.`;
        else if (a.status === "advanced") anchorMsg = ` ${a.new_rows} new row(s) since the last check; saved head updated.`;
        else if (a.status === "unavailable") anchorMsg = " Could not use the saved head file (" + (a.detail || "unavailable") + ").";
        const n = v.noted ? ` Your noted head is row ${v.noted.found_at_row} of the chain (${v.noted.rows_after_it} newer).` : "";
        box.className = "au-verify ok";
        box.textContent = `Chain intact. ${v.checked} protected row(s) checked, none edited or removed.` + anchorMsg + n
          + (v.head_full ? ` Head hash (note it somewhere off this server): ${v.head_full}` : "") + old;
      } else {
        box.className = "au-verify bad";
        box.textContent = (v.broken_at ? `Chain broken at event #${v.broken_at}. ` : "Chain check failed. ") + v.reason + "." + old;
      }
    } catch (e) { box.className = "au-verify bad"; box.textContent = "Could not verify: " + e.message; }
  }

  async function exportCsv() {
    const p = new URLSearchParams(params(0));
    p.delete("limit"); p.delete("offset");
    try {
      const res = await fetch("/audit/export.csv?" + p.toString(), { headers: { Authorization: "Bearer " + TOKEN } });
      if (!res.ok) throw new Error(res.status === 403 ? "Manager role or above required" : "Export failed");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = (res.headers.get("Content-Disposition") || "").split("filename=")[1]?.replace(/"/g, "") || "audit-log.csv";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      if (window.toast) toast("CSV downloaded", "ok");
    } catch (e) { if (window.toast) toast(e.message, "error"); else alert(e.message); }
  }

  function rowHtml(e) {
    return `<div class="card au-row" data-au="${e.id}">
      <div class="au-top"><span class="au-time">${E(fmt(e.created_at))}</span>${badge(e.category)}
        <span><strong>${E(e.actor)}</strong> ${E(e.summary)}</span></div>
      ${e.detail ? `<div class="au-detail-text">${E(e.detail)}</div>` : ""}
      <div class="au-panel hidden"></div></div>`;
  }

  async function load(reset) {
    build();
    const my = ++seq;
    if (reset) { offset = 0; loaded = []; }
    let r;
    try { r = await api("/audit/events?" + params(offset)); }
    catch (e) {
      if (my === seq) { $("au-count").textContent = ""; $("au-list").innerHTML = `<div class="empty-note">${/role|403/i.test(e.message) ? "Manager role or above required." : "Audit log unavailable: " + E(e.message)}</div>`; }
      return;
    }
    if (my !== seq) return;
    loaded = loaded.concat(r.events);
    offset = loaded.length;
    const cur = $("au-actor").value;
    $("au-actor").innerHTML = `<option value="">Any person</option>` + r.actors.map(a => `<option value="${a.id}">${E(a.name)}</option>`).join("");
    $("au-actor").value = cur;
    const all = r.categories.reduce((n, c) => n + c.count, 0);
    $("au-chips").innerHTML = `<button class="au-chip ${cat ? "" : "on"}" data-cat="">All (${all})</button>` +
      r.categories.map(c => `<button class="au-chip ${cat === c.name ? "on" : ""}" data-cat="${E(c.name)}">${E(c.name)} (${c.count})</button>`).join("");
    $("au-count").textContent = `${r.total} event(s)` + (cat ? ` in ${cat}` : "");
    $("au-list").innerHTML = loaded.length ? loaded.map(rowHtml).join("") : `<div class="empty-note">No events match these filters.</div>`;
    $("au-more").classList.toggle("hidden", loaded.length >= r.total);
  }

  function mini(list) {
    return list.length ? list.map(x => `<div class="au-mini">${E(fmt(x.created_at))} &middot; <strong>${E(x.actor)}</strong> ${E(x.summary)}${x.detail ? " &mdash; " + E(x.detail) : ""}</div>`).join("")
      : `<div class="au-mini">None.</div>`;
  }

  async function toggle(row) {
    const panel = row.querySelector(".au-panel");
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    panel.innerHTML = `<div class="au-mini">Loading&hellip;</div>`;
    try {
      const j = await api("/audit/events/" + row.dataset.au);
      const ev = j.event;
      panel.innerHTML = `
        <div class="kv" style="display:grid;grid-template-columns:120px 1fr;gap:4px 10px">
          <div class="k">When</div><div>${E(fmt(ev.created_at))}</div>
          <div class="k">Who</div><div>${E(ev.actor)}${ev.actor_role ? " (" + E(ev.actor_role) + ")" : ""}</div>
          <div class="k">Category</div><div>${E(ev.category)}</div>
          <div class="k">Action</div><div><code>${E(j.raw.action)}</code></div>
          <div class="k">Details</div><div>${E(j.raw.detail || "—")}</div></div>
        <div style="margin-top:8px"><strong>Other events about the same thing</strong>${mini(j.same_subject)}</div>
        <div style="margin-top:8px"><strong>Same person, within 5 minutes</strong>${mini(j.same_actor_within_5_minutes)}</div>`;
    } catch (e) { panel.innerHTML = `<div class="au-mini">Could not load details: ${E(e.message)}</div>`; }
  }

  window.FEATURE_VIEWS = Object.assign(window.FEATURE_VIEWS || {}, { audit: () => load(true) });
})();
