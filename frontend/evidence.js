// Anamnesis Evidence Map, "Search vs Anamnesis" and the Knowledge Graph upgrade.
// Plugin pattern like brain.js / search.js: one IIFE, hooks decorateAnswer, registers FEATURE_VIEWS.graph.
// Talks to GET /ask/{id}/evidence, GET /evidence/compare and GET /evidence/graph (all permission-filtered on the server).
// The browser never decides what a user may see; it only draws what the server returned.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const E = s => esc(s == null ? "" : String(s)).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const reduced = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const DEFAULT_Q = "How often does the VPN password rotate?";

  // ------------------------------------------------------------------ icons ---
  // Original line icons, drawn inline. No brand logos.
  const P = {
    document: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/><path d="M10 12h5M10 16h5"/>',
    fact: '<circle cx="6" cy="7" r="2.2"/><circle cx="18" cy="7" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M8.2 7h7.6M7 9l4 7M17 9l-4 7"/>',
    verified: '<path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    review: '<path d="M6 21V4"/><path d="M6 4h11l-2 4 2 4H6"/>',
    conflict: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
    used: '<path d="M9 4h6l-1 6 3 3H7l3-3z"/><path d="M12 13v7"/>'
  };
  const icon = t => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${P[t] || P.document}</svg>`;
  const TYPE_LABEL = { document: "Document", fact: "Fact", verified: "Verified answer", review: "Review", conflict: "Conflict", used: "Used for work" };

  const day = iso => {
    if (!iso) return "";
    const d = new Date(iso + (/(z|[+-]\d\d:?\d\d)$/i.test(iso) ? "" : "Z"));
    return isNaN(d) ? "" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  };

  // ------------------------------------------------------------- the map ---
  function cardHtml(c) {
    const link = c.link ? ` data-link="${E(JSON.stringify(c.link))}" role="link"` : ' role="group"';
    const first = (c.lines || [])[0] || "";
    const aria = `${TYPE_LABEL[c.type] || "Source"}: ${c.title}. ${c.status_label}. ${first}`;
    const ver = c.versions ? `<div class="ev-ver">${c.versions.map(v => `<div><span>${E(v.label)}</span><b title="${E(v.text)}">${E(v.text)}</b></div>`).join("")}</div>` : "";
    return `<article class="ev-card ev-t-${E(c.type)} ev-s-${E(c.status)}" data-card="${E(c.id)}" tabindex="0"${link} aria-label="${E(aria)}">
      <div class="ev-h"><span class="ev-ico">${icon(c.type)}</span><span class="ev-t" title="${E(c.title)}">${E(c.title)}</span><span class="ev-chip ev-c-${E(c.status)}">${E(c.status_label)}</span></div>
      ${ver}${(c.lines || []).slice(0, 3).map(l => `<p class="ev-l" title="${E(l)}">${E(l)}</p>`).join("")}
      ${c.quote ? `<p class="ev-qt" title="${E(c.quote)}">${E(c.quote)}</p>` : ""}
      <div class="ev-d">${E(day(c.date))}</div></article>`;
  }

  function mapHtml(d, o) {
    o = o || {};
    const st = d.state || {};
    const cards = d.cards || [];
    return `<div class="ev-map${o.compact ? " ev-compact" : ""}" data-edges="${E(JSON.stringify(d.edges || []))}">
      <svg class="ev-lines" aria-hidden="true" focusable="false"></svg>
      ${o.compact ? "" : `<div class="ev-q" title="${E(d.question)}">${E(d.question)}</div>`}
      <div class="ev-a"><div class="ev-a-t">${E(d.answer || "No answer was found in what you may open.")}</div>
        ${st.label ? `<span class="ev-state ev-st-${E(st.code)}">${E(st.label)}</span>` : ""}
        ${st.detail && !o.compact ? `<div class="ev-a-d">${E(st.detail)}</div>` : ""}</div>
      ${cards.length ? `<div class="ev-grid" role="list" aria-label="Sources behind this answer">${cards.map(cardHtml).join("")}</div>`
        : `<div class="ev-note">No source you may open backs this answer.</div>`}
      ${o.hint ? `<div class="ev-note">${E(o.hint)}</div>` : ""}
    </div>`;
  }

  // Lines are drawn from measured card positions, and only for edges the server returned.
  function drawLines(map) {
    const svg = map.querySelector(".ev-lines");
    if (!svg) return;
    svg.innerHTML = "";
    if (getComputedStyle(svg).display === "none") return;              // mobile: cards stack, lines hidden
    let edges = [];
    try { edges = JSON.parse(map.dataset.edges || "[]"); } catch (e) { /* none */ }
    const mb = map.getBoundingClientRect();
    if (!edges.length || mb.width < 10) return;
    svg.setAttribute("viewBox", `0 0 ${mb.width} ${mb.height}`);
    const rel = el => { const r = el.getBoundingClientRect(); return { l: r.left - mb.left, r: r.right - mb.left, t: r.top - mb.top, b: r.bottom - mb.top, cx: (r.left + r.right) / 2 - mb.left, cy: (r.top + r.bottom) / 2 - mb.top }; };
    const ansEl = map.querySelector(".ev-a");
    const gridEl = map.querySelector(".ev-grid");
    if (!ansEl || !gridEl) return;
    const ans = rel(ansEl), grid = rel(gridEl);
    const box = {};
    map.querySelectorAll(".ev-card").forEach(el => { box[el.dataset.card] = rel(el); });
    const tops = [...new Set(Object.values(box).map(b => Math.round(b.t / 4)))].sort((a, b) => a - b);
    Object.values(box).forEach(b => { b.row = tops.indexOf(Math.round(b.t / 4)); });
    const rowBottom = i => Math.max(...Object.values(box).filter(b => b.row === i).map(b => b.b));
    const rowTop = i => Math.min(...Object.values(box).filter(b => b.row === i).map(b => b.t));
    const cs = getComputedStyle(gridEl);
    const colGap = parseFloat(cs.columnGap) || 24;
    const busY = ans.b + Math.max(8, (grid.t - ans.b) / 2);
    const paths = [], dots = [];
    const dot = (x, y, kind) => dots.push(`<circle class="ev-dot ev-dot-${kind}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3"/>`);
    const path = (d, kind) => paths.push(`<path class="ev-edge ev-e-${kind}" d="${d}"/>`);
    const f = n => n.toFixed(1);

    edges.forEach(e => {
      const a = box[e.from];
      if (!a) return;
      if (e.to === "answer") {
        let d = `M${f(a.cx)} ${f(a.t)}`;
        if (a.row > 0) {                                              // leave through the gutter so we never cross a card above
          const gy = (rowBottom(a.row - 1) + rowTop(a.row)) / 2;
          const rightmost = !Object.values(box).some(b => b.row === a.row && b.l > a.r);
          const gx = rightmost ? a.l - colGap / 2 : a.r + colGap / 2;
          d += ` V${f(gy)} H${f(gx)} V${f(busY)}`;
        } else d += ` V${f(busY)}`;
        d += ` H${f(ans.cx)} V${f(ans.b)}`;
        path(d, e.kind); dot(a.cx, a.t, e.kind); dot(ans.cx, ans.b, e.kind);
        return;
      }
      const b = box[e.to];
      if (!b) return;
      if (a.row === b.row) {
        const [L, R] = a.cx <= b.cx ? [a, b] : [b, a];
        const between = Object.values(box).some(x => x.row === a.row && x !== a && x !== b && x.cx > L.cx && x.cx < R.cx);
        if (!between) {
          path(`M${f(L.r)} ${f(L.cy)} H${f(R.l)}`, e.kind); dot(L.r, L.cy, e.kind); dot(R.l, R.cy, e.kind);
        } else {
          const below = a.row === 0;
          const y = below ? a.b + 14 : a.t - 14;
          path(`M${f(a.cx)} ${f(below ? a.b : a.t)} V${f(y)} H${f(b.cx)} V${f(below ? b.b : b.t)}`, e.kind);
          dot(a.cx, below ? a.b : a.t, e.kind); dot(b.cx, below ? b.b : b.t, e.kind);
        }
      } else {
        const [U, D] = a.row < b.row ? [a, b] : [b, a];
        const gy = (rowBottom(U.row) + rowTop(D.row)) / 2;
        path(`M${f(U.cx)} ${f(U.b)} V${f(gy)} H${f(D.cx)} V${f(D.t)}`, e.kind);
        dot(U.cx, U.b, e.kind); dot(D.cx, D.t, e.kind);
      }
    });
    svg.innerHTML = paths.join("") + dots.join("");
  }

  const seen = new WeakSet();
  function mount(el, d, o) {
    el.innerHTML = mapHtml(d, o);
    const map = el.querySelector(".ev-map");
    const redraw = () => drawLines(map);
    requestAnimationFrame(() => { redraw(); requestAnimationFrame(redraw); });
    if (window.ResizeObserver && !seen.has(map)) {                      // also fires when a hidden section becomes visible
      seen.add(map);
      let t = null;
      new ResizeObserver(() => { clearTimeout(t); t = setTimeout(redraw, 30); }).observe(map);
    }
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(redraw).catch(() => {});
    return map;
  }

  // Card navigation: documents open in the viewer (server re-checks access), facts jump to the graph node.
  function follow(link) {
    if (!link) return;
    const ov = document.querySelector(".ev-overlay"); if (ov) ov.remove();
    if (link.kind === "document") openDoc(Number(link.id), link.highlight || undefined);
    else if (link.kind === "node") focusNode(link.label);
    else if (link.kind === "view") showView(link.view);
  }
  document.addEventListener("click", e => {
    const c = e.target.closest(".ev-card[data-link]"); if (!c) return;
    try { follow(JSON.parse(c.dataset.link)); } catch (err) { /* ignore */ }
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const c = e.target.closest && e.target.closest(".ev-card[data-link]"); if (!c || e.target !== c) return;
    e.preventDefault();
    try { follow(JSON.parse(c.dataset.link)); } catch (err) { /* ignore */ }
  });
  window.addEventListener("resize", () => {
    clearTimeout(window.__evRz);
    window.__evRz = setTimeout(() => document.querySelectorAll(".ev-map").forEach(drawLines), 80);
  });

  // ---------------------------------------------------- panel under answers ---
  const prevAnswer = window.decorateAnswer;
  window.decorateAnswer = function (m, h) {
    if (prevAnswer) h = prevAnswer(m, h);
    if (!m || !m.qa_id || !(m.answered && (m.answer || m.verified || m.answer_plain))) return h;
    const panel = `<details class="ev-panel" data-qa="${E(m.qa_id)}"><summary>Evidence<span class="ev-sub">where this answer comes from</span></summary><div class="ev-body"></div></details>`;
    const i = h.indexOf('<div class="action-row">');
    return i >= 0 ? h.slice(0, i) + panel + h.slice(i) : h + panel;
  };

  document.addEventListener("toggle", async e => {                        // 'toggle' does not bubble, so listen in the capture phase
    const d = e.target;
    if (!d || !d.classList || !d.classList.contains("ev-panel") || !d.open || d.dataset.loaded) return;
    d.dataset.loaded = "1";
    const body = d.querySelector(".ev-body");
    body.innerHTML = '<div class="ev-load" role="status">Loading evidence</div>';
    const qa = d.dataset.qa;
    try {
      const data = await api(`/ask/${encodeURIComponent(qa)}/evidence`);
      let hint = "";
      try { if (typeof MSGS !== "undefined" && MSGS[qa] && MSGS[qa].access_hint) hint = "Another source may exist that you cannot open. Use Request access under the answer."; } catch (err) { /* none */ }
      mount(body, data, { hint });
    } catch (err) {
      d.dataset.loaded = "";
      body.innerHTML = `<div class="ev-load">Evidence is unavailable: ${E(err.message)}</div>`;
    }
  }, true);

  // ------------------------------------------------------ Search vs Anamnesis ---
  function compareHtml(plain, anam, o) {
    o = o || {};
    const max = Math.max(1, ...plain.rows.map(r => r.count));
    const firstDoc = (anam.cards || []).find(c => c.type === "document");
    return `<div class="ev-cmp">
      <section class="ev-pan" aria-label="Plain search">${o.illustration ? '<span class="ev-tag">Illustration</span>' : ""}
        <h3>Plain search</h3><div class="ev-box" title="${E(o.query)}">${E(o.query)}</div>
        <div class="ev-big"><b>${E(plain.total)}</b>${plain.total === 1 ? "result" : "results"}</div>
        <div class="ev-rows">${plain.rows.map(r => `<div class="ev-row"><span>${E(r.label)}</span><div class="ev-bar"><i style="width:${Math.round(100 * r.count / max)}%"></i></div><em>${E(r.count)}</em></div>`).join("")}</div>
        <div class="ev-miss"><b>What is missing</b><ul>${plain.missing.map(x => `<li>${E(x)}</li>`).join("")}</ul></div>
        <p class="ev-small">${E(plain.note || "")}</p></section>
      <div class="ev-vs" aria-hidden="true"><span>VS</span></div>
      <section class="ev-pan" aria-label="Anamnesis">${o.illustration ? '<span class="ev-tag">Illustration</span>' : ""}
        <h3>Anamnesis</h3><div class="ev-box" title="${E(o.query)}">${E(o.query)}</div>
        <div class="ev-ans">${E(anam.answer || "No answer was found in what you may open.")}
          <small>${firstDoc ? "Cited: " + E(firstDoc.title) : "No cited document"}</small></div>
        <div class="ev-mini"></div></section>
    </div><p class="ev-cap">Search returns documents. Anamnesis returns understanding.</p>`;
  }

  function renderCompare(host, data, o) {
    host.innerHTML = compareHtml(data.plain, data.anamnesis, o);
    mount(host.querySelector(".ev-mini"), data.anamnesis, { compact: true });
  }

  function openCompare() {
    const typed = ($("ask-input") && $("ask-input").value.trim()) || "";
    const ov = document.createElement("div");
    ov.className = "ev-overlay";
    ov.innerHTML = `<div class="ev-modal" role="dialog" aria-modal="true" aria-labelledby="ev-cmp-h">
      <div class="ev-modal-h"><h3 id="ev-cmp-h">Search vs Anamnesis</h3><button class="btn-ghost" data-x type="button">Close</button></div>
      <form class="ev-run"><input type="text" id="ev-cmp-q" aria-label="Question" value="${E(typed || DEFAULT_Q)}" maxlength="300"><button class="btn-primary" type="submit" style="margin:0">Compare</button></form>
      <div id="ev-cmp-out" aria-live="polite"><div class="ev-load">Loading</div></div></div>`;
    const prevFocus = document.activeElement;
    document.body.appendChild(ov);
    const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); if (prevFocus && prevFocus.focus) prevFocus.focus(); };
    const onKey = e => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    ov.addEventListener("click", e => { if (e.target === ov || e.target.closest("[data-x]")) close(); });
    const out = ov.querySelector("#ev-cmp-out");
    let seq = 0;
    async function run() {
      const q = ov.querySelector("#ev-cmp-q").value.trim();
      if (q.length < 2) return;
      const my = ++seq;
      out.innerHTML = '<div class="ev-load">Loading</div>';
      try {
        const data = await api("/evidence/compare?q=" + encodeURIComponent(q));
        if (my === seq) renderCompare(out, data, { query: q });
      } catch (err) { if (my === seq) out.innerHTML = `<div class="ev-load">Comparison is unavailable: ${E(err.message)}</div>`; }
    }
    ov.querySelector("form").addEventListener("submit", e => { e.preventDefault(); run(); });
    ov.querySelector("#ev-cmp-q").focus();
    run();
  }

  const head = document.querySelector("#view-ask .chat-head");
  if (head) {
    head.insertAdjacentHTML("beforeend", '<button class="btn-ghost" id="ask-evcmp-btn" type="button">Search vs Anamnesis</button>');
    $("ask-evcmp-btn").addEventListener("click", openCompare);
  }

  // Landing page section. Numbers are labelled "Illustration": they were counted once from the freshly seeded demo database.
  const LANDING = {
    query: DEFAULT_Q,
    plain: { total: 2, rows: [{ label: "Documents", count: 1 }, { label: "Passages", count: 1 }, { label: "Facts", count: 0 }],
      missing: ["No context", "No relationships", "No proof"],
      note: "Illustration. Counted from the freshly seeded demo database; in the app the numbers are counted from your own documents." },
    anamnesis: {
      answer: "The VPN password rotates every 90 days.", question: DEFAULT_Q,
      state: { code: "verified", label: "Verified by a person" },
      cards: [
        { id: "doc-demo", type: "document", title: "VPN Access Policy (IT)", lines: ["IT \u00b7 Internal \u00b7 120 days old \u00b7 may be outdated"],
          quote: "The VPN password rotates every 90 days and must be reset through the helpdesk portal.", status: "stale", status_label: "Stale", date: null, link: null },
        { id: "rev-demo", type: "review", title: "Review", lines: ["Flagged: rotation period may be outdated", "Decided by the IT manager"], status: "review", status_label: "Reviewed", date: null, link: null },
        { id: "ver-demo", type: "verified", title: "Verified answer", lines: ["Reviewed by IT manager"], status: "verified", status_label: "Verified", date: null, link: null }
      ],
      edges: [{ from: "doc-demo", to: "answer", kind: "cites" }, { from: "rev-demo", to: "answer", kind: "reviewed" }, { from: "ver-demo", to: "answer", kind: "reviewed" }]
    }
  };
  const win = document.querySelector("#landing .lp-win");
  if (win) {
    win.insertAdjacentHTML("afterend", `<section class="ev-lp" id="lp-vs"><h2>Search returns documents. Anamnesis returns understanding.</h2>
      <p>The same question, two ways. Plain search hands you a pile to read. Anamnesis gives one cited answer and shows the sources, their age and who checked them.</p>
      <div id="lp-vs-host"></div></section>`);
    renderCompare($("lp-vs-host"), LANDING, { query: LANDING.query, illustration: true });
  }

  // --------------------------------------------------------- knowledge graph ---
  const G = { net: null, nodes: null, edges: null, data: null, sel: null, hl: null, pending: null, shown: null };
  const COL = {
    node: { background: "#0b1511", border: "#34d399" }, nodeC: { background: "#2a0f12", border: "#f87171" },
    dimNode: { background: "rgba(11,21,17,.35)", border: "rgba(52,211,153,.16)" }, dimNodeC: { background: "rgba(42,15,18,.35)", border: "rgba(248,113,113,.2)" },
    edge: "#1f7a57", edgeC: "#f87171", dimEdge: "rgba(31,122,87,.14)", dimEdgeC: "rgba(248,113,113,.16)"
  };
  const FONT = '"Schibsted Grotesk","Segoe UI",Roboto,Arial,sans-serif';

  function graphDom() {
    if ($("evg")) return;
    const old = $("graph-canvas");
    old.style.display = "none";
    old.insertAdjacentHTML("afterend", `<div id="evg" class="evg">
      <div class="evg-bar">
        <div class="evg-search"><input type="text" id="evg-q" placeholder="Search nodes" aria-label="Search nodes" autocomplete="off"><div id="evg-hits" class="evg-hits"></div></div>
        <select id="evg-status" aria-label="Filter by status"><option value="">All statuses</option><option value="ok">Current</option><option value="conflict">Conflict</option></select>
        <select id="evg-dept" aria-label="Filter by department"><option value="">All departments</option></select>
        <div class="evg-tools">
          <button class="btn-ghost" type="button" id="evg-zin" aria-label="Zoom in">Zoom in</button>
          <button class="btn-ghost" type="button" id="evg-zout" aria-label="Zoom out">Zoom out</button>
          <button class="btn-ghost" type="button" id="evg-fit" aria-label="Fit graph to view">Fit</button>
          <button class="btn-ghost" type="button" id="evg-lay" aria-label="Re-layout graph">Re-layout</button>
        </div></div>
      <div class="evg-bar"><input type="text" id="evg-from" list="evg-names" placeholder="Path from" aria-label="Path from node" autocomplete="off">
        <input type="text" id="evg-to" list="evg-names" placeholder="Path to" aria-label="Path to node" autocomplete="off">
        <datalist id="evg-names"></datalist>
        <button class="btn-secondary" type="button" id="evg-find">Find path</button><button class="btn-ghost" type="button" id="evg-clear">Clear</button></div>
      <div id="evg-path" class="evg-pathout" aria-live="polite"></div>
      <div class="evg-main"><div id="evg-canvas" class="evg-canvas" role="img" aria-label="Knowledge graph. Use the search box to pick a node."></div>
        <aside id="evg-side" class="evg-side" aria-live="polite"><h3>Details</h3><p class="muted">Click a node, or search for one, to see its facts, who added them and the source document.</p></aside></div>
      <div class="evg-legend"><span><i></i>Node</span><span><i class="c"></i>Node in a conflict</span><span><i class="l"></i>Fact</span><span><i class="lc"></i>Conflicting fact</span></div></div>`);
    $("evg-q").addEventListener("input", onSearch);
    $("evg-q").addEventListener("keydown", e => {
      if (e.key === "Escape") { e.target.value = ""; onSearch(); clearHighlight(); }
      if (e.key === "Enter") { const b = $("evg-hits").querySelector("button"); if (b) b.click(); }
    });
    $("evg-hits").addEventListener("click", e => { const b = e.target.closest("[data-n]"); if (b) { $("evg-hits").innerHTML = ""; selectNode(b.dataset.n); } });
    $("evg-status").addEventListener("change", rebuild);
    $("evg-dept").addEventListener("change", rebuild);
    $("evg-zin").addEventListener("click", () => zoom(1.25));
    $("evg-zout").addEventListener("click", () => zoom(0.8));
    $("evg-fit").addEventListener("click", () => G.net && G.net.fit({ animation: reduced() ? false : { duration: 300 } }));
    $("evg-lay").addEventListener("click", relayout);
    $("evg-find").addEventListener("click", findPath);
    $("evg-clear").addEventListener("click", () => { $("evg-from").value = ""; $("evg-to").value = ""; $("evg-path").innerHTML = ""; clearHighlight(); });
    $("evg-side").addEventListener("click", e => {
      const doc = e.target.closest("[data-doc]");
      if (doc) { e.preventDefault(); openDoc(Number(doc.dataset.doc)); return; }
      const f = e.target.closest("[data-pfrom]"); if (f) { $("evg-from").value = f.dataset.pfrom; $("evg-to").focus(); }
      const t = e.target.closest("[data-pto]"); if (t) { $("evg-to").value = t.dataset.pto; if ($("evg-from").value) findPath(); else $("evg-from").focus(); }
    });
  }

  function visibleSets() {
    const st = $("evg-status").value, dp = $("evg-dept").value;
    const edges = G.data.edges.filter(e => (!st || (st === "conflict" ? e.status === "conflicting" : e.status !== "conflicting")) && (!dp || e.department === dp));
    const ids = new Set();
    edges.forEach(e => { ids.add(e.from); ids.add(e.to); });
    return { edges, nodes: G.data.nodes.filter(n => ids.has(n.id)) };
  }

  function nodeStyle(n, dim) {
    const c = n.conflict ? (dim ? COL.dimNodeC : COL.nodeC) : (dim ? COL.dimNode : COL.node);
    return { id: n.id, color: { background: c.background, border: c.border, highlight: { background: c.background, border: "#86efac" }, hover: { background: c.background, border: "#86efac" } },
      font: { color: dim ? "rgba(241,247,243,.28)" : "#f1f7f3", face: FONT, size: 13 } };
  }
  function edgeStyle(e, dim) {
    const bad = e.status === "conflicting";
    return { id: "e" + e.id, color: { color: dim ? (bad ? COL.dimEdgeC : COL.dimEdge) : (bad ? COL.edgeC : COL.edge), highlight: "#34d399", hover: "#34d399" },
      font: { color: dim ? "rgba(139,154,146,.25)" : "#8b9a92", size: 11, strokeWidth: 0, face: FONT }, width: dim ? 1 : (G.hl && G.hl.edges.has(e.id) ? 2 : 1) };
  }

  function rebuild() {
    if (!G.data) return;
    G.hl = null;
    const v = visibleSets();
    G.shown = v;
    const nodes = new vis.DataSet(v.nodes.map(n => Object.assign(nodeStyle(n, false), { label: n.label, shape: "box", margin: 9, borderWidth: 1, shapeProperties: { borderRadius: 6 } })));
    const edges = new vis.DataSet(v.edges.map(e => Object.assign(edgeStyle(e, false), { from: e.from, to: e.to, label: e.label, arrows: { to: { enabled: true, scaleFactor: 0.6 } }, smooth: { type: "dynamic" } })));
    G.nodes = nodes; G.edges = edges;
    const opts = { physics: { enabled: true, solver: "forceAtlas2Based", forceAtlas2Based: { gravitationalConstant: -70, springLength: 130 }, stabilization: { iterations: 200 } },
      interaction: { hover: true, keyboard: { enabled: true }, tooltipDelay: 250 }, layout: { improvedLayout: v.nodes.length < 150 } };
    if (G.net) G.net.destroy();
    G.net = new vis.Network($("evg-canvas"), { nodes, edges }, opts);
    G.net.once("stabilizationIterationsDone", () => { G.net.setOptions({ physics: false }); G.net.fit({ animation: false }); });
    G.net.on("click", p => {
      if (p.nodes.length) selectNode(p.nodes[0]);
      else if (p.edges.length) { const e = G.data.edges.find(x => "e" + x.id === p.edges[0]); if (e) selectNode(e.from, e.id); }
      else clearHighlight();
    });
    $("evg-names").innerHTML = v.nodes.map(n => `<option value="${E(n.label)}"></option>`).join("");
    if (G.sel && !v.nodes.some(n => n.id === G.sel)) { G.sel = null; sidePlaceholder(); }
  }

  function apply(hl) {                                                    // hl = {nodes:Set, edges:Set} or null (clear dimming)
    G.hl = hl;
    if (!G.nodes) return;
    G.nodes.update(G.shown.nodes.map(n => nodeStyle(n, !!hl && !hl.nodes.has(n.id))));
    G.edges.update(G.shown.edges.map(e => edgeStyle(e, !!hl && !hl.edges.has(e.id))));
  }
  function clearHighlight() { G.sel = null; apply(null); sidePlaceholder(); }
  function sidePlaceholder() { $("evg-side").innerHTML = '<h3>Details</h3><p class="muted">Click a node, or search for one, to see its facts, who added them and the source document.</p>'; }

  function selectNode(id, factId) {
    if (!G.data) return;
    const node = G.data.nodes.find(n => n.id === id);
    if (!node) return;
    G.sel = id;
    const inc = G.data.edges.filter(e => e.from === id || e.to === id);
    const hl = { nodes: new Set([id]), edges: new Set() };
    G.shown.edges.filter(e => e.from === id || e.to === id).forEach(e => { hl.edges.add(e.id); hl.nodes.add(e.from); hl.nodes.add(e.to); });
    apply(hl);
    if (G.net && G.nodes.get(id)) G.net.focus(id, { scale: Math.max(G.net.getScale(), 0.9), animation: reduced() ? false : { duration: 300 } });
    const bad = node.conflict;
    inc.sort((a, b) => (b.status === "conflicting") - (a.status === "conflicting"));
    $("evg-side").innerHTML = `<h3>${E(node.label)}</h3>
      <span class="ev-chip ${bad ? "ev-c-conflict" : "ev-c-current"}">${bad ? "Conflict" : "Current"}</span>
      <p class="muted">${inc.length} ${inc.length === 1 ? "fact" : "facts"} connect to this node</p>
      ${inc.map(e => `<div class="evg-fact${e.status === "conflicting" ? " conflict" : ""}${factId === e.id ? " on" : ""}">
        <b>${E(e.from)} ${E(e.label)} ${E(e.to)}</b>
        <span class="ev-chip ${e.status === "conflicting" ? "ev-c-conflict" : "ev-c-current"}">${e.status === "conflicting" ? "Conflict" : "Current"}</span>
        <div class="muted">${e.added_by ? "Added by " + E(e.added_by) : "Added by the system"}${e.created_at ? " \u00b7 " + E(day(e.created_at)) : ""}</div>
        ${e.doc ? `<div>Source: <a href="#" data-doc="${E(e.doc.id)}">${E(e.doc.title)}</a> <span class="muted">${E(e.doc.department)}</span></div>` : ""}</div>`).join("")}
      <div class="evg-act"><button class="btn-ghost" type="button" data-pfrom="${E(node.label)}">Path from here</button><button class="btn-ghost" type="button" data-pto="${E(node.label)}">Path to here</button>
        ${bad ? '<button class="btn-ghost" type="button" onclick="showView(\'conflicts\')">Open conflicts</button>' : ""}</div>`;
  }

  function onSearch() {
    const q = $("evg-q").value.trim().toLowerCase();
    if (!q || !G.shown) { $("evg-hits").innerHTML = ""; if (!q) apply(null); return; }
    const hits = G.shown.nodes.filter(n => n.label.toLowerCase().includes(q));
    $("evg-hits").innerHTML = hits.slice(0, 8).map(n => `<button type="button" data-n="${E(n.id)}">${E(n.label)}</button>`).join("");
    apply({ nodes: new Set(hits.map(n => n.id)), edges: new Set() });
  }

  function zoom(f) { if (G.net) G.net.moveTo({ scale: G.net.getScale() * f, animation: reduced() ? false : { duration: 200 } }); }
  function relayout() {
    if (!G.net) return;
    G.net.setOptions({ physics: { enabled: true } });
    G.net.once("stabilized", () => { G.net.setOptions({ physics: false }); G.net.fit({ animation: false }); });
    G.net.stabilize(200);
  }

  // Shortest chain of real facts between two nodes. Directed first (the way Ask walks subject -> object), then ignoring direction.
  function findChain(from, to, edges) {
    const walk = directed => {
      const prev = new Map([[from, null]]), q = [from];
      while (q.length) {
        const cur = q.shift();
        if (cur === to) break;
        for (const e of edges) {
          let nxt = null, back = false;
          if (e.from === cur) nxt = e.to; else if (!directed && e.to === cur) { nxt = e.from; back = true; }
          if (nxt != null && !prev.has(nxt)) { prev.set(nxt, { e, from: cur, back }); q.push(nxt); }
        }
      }
      if (!prev.has(to)) return null;
      const steps = [];
      for (let n = to; prev.get(n); n = prev.get(n).from) steps.unshift(prev.get(n));
      return steps;
    };
    const d = walk(true);
    if (d) return { steps: d, directed: true };
    const u = walk(false);
    return u ? { steps: u, directed: false } : null;
  }

  function findPath() {
    if (!G.shown) return;
    const pick = v => {
      const s = v.trim().toLowerCase(); if (!s) return null;
      const all = G.shown.nodes;
      return (all.find(n => n.label.toLowerCase() === s) || (all.filter(n => n.label.toLowerCase().includes(s)).length === 1 ? all.find(n => n.label.toLowerCase().includes(s)) : null) || {}).id || null;
    };
    const a = pick($("evg-from").value), b = pick($("evg-to").value), out = $("evg-path");
    if (!a || !b) { out.innerHTML = '<div class="evg-chain">Pick two nodes from the list. Type a few letters and choose one.</div>'; return; }
    if (a === b) { out.innerHTML = '<div class="evg-chain">Those are the same node.</div>'; return; }
    const r = findChain(a, b, G.shown.edges);
    if (!r) { out.innerHTML = `<div class="evg-chain">No chain of facts connects <b>${E(a)}</b> and <b>${E(b)}</b> in what you can see.</div>`; apply(null); return; }
    const hl = { nodes: new Set([a]), edges: new Set() };
    r.steps.forEach(s => { hl.nodes.add(s.e.from); hl.nodes.add(s.e.to); hl.edges.add(s.e.id); });
    apply(hl);
    G.sel = null;
    if (G.net) G.net.fit({ nodes: [...hl.nodes], animation: reduced() ? false : { duration: 300 } });
    const text = r.steps.map(s => `${E(s.from)} <b>${E(s.e.label)}${s.back ? " (against the arrow)" : ""}</b> ${E(s.back ? s.e.from : s.e.to)}`).join(", then ");
    out.innerHTML = `<div class="evg-chain">${r.directed ? "Chain of facts" : "Connected, ignoring direction"} (${r.steps.length} ${r.steps.length === 1 ? "hop" : "hops"}): ${text}</div>`;
  }

  async function loadGraph2() {
    graphDom();
    let d;
    try { d = await api("/evidence/graph"); } catch (e) { $("evg-canvas").innerHTML = `<div class="empty-note">The graph is unavailable: ${E(e.message)}</div>`; return; }
    G.data = d;
    if (!d.nodes.length) { $("evg-canvas").innerHTML = "<div class='empty-note'>No facts yet. Add one above.</div>"; return; }
    const dept = $("evg-dept"), cur = dept.value;
    dept.innerHTML = '<option value="">All departments</option>' + d.departments.map(x => `<option value="${E(x)}">${E(x)}</option>`).join("");
    dept.value = d.departments.includes(cur) ? cur : "";
    rebuild();
    if (G.pending) { const p = G.pending; G.pending = null; setTimeout(() => selectNode(p), 60); }
  }
  function focusNode(label) { G.pending = label; showView("graph"); }

  window.FEATURE_VIEWS = Object.assign(window.FEATURE_VIEWS || {}, { graph: loadGraph2 });
  window.loadGraph = loadGraph2;                                          // app.js calls loadGraph() after "Add fact"
})();
