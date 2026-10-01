// Anamnesis Search page. Talks to GET /search (server-side permission gate; filter lists come from the server
// and only describe documents the signed-in user may open). Plugs in through FEATURE_VIEWS like brain.js.
(function () {
  const $ = id => document.getElementById(id);
  const E = s => esc(s == null ? "" : String(s)).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  let timer = null, seq = 0;

  const parent = $("view-health").parentNode;
  parent.insertAdjacentHTML("beforeend", `
    <section id="view-search" class="view"><h2>Search</h2>
      <div class="muted" style="margin-bottom:10px">Search everything you are allowed to open. Results and filter options only include documents your role and department can access.</div>
      <div class="card" style="padding:14px">
        <input type="text" id="sr-q" placeholder="Search knowledge, e.g. vpn password, leave policy, postgres migration">
        <div class="sr-filters" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:10px">
          <select id="sr-dept"><option value="">Any department</option></select>
          <select id="sr-tag"><option value="">Any tag</option></select>
          <select id="sr-owner"><option value="">Any owner</option></select>
          <select id="sr-vis"><option value="">Any visibility</option></select>
          <input type="date" id="sr-from" title="Created on or after">
          <input type="date" id="sr-to" title="Created on or before">
        </div>
        <div style="margin-top:10px"><button class="btn-ghost" id="sr-clear">Clear filters</button></div>
      </div>
      <div id="sr-count" class="muted" style="margin:12px 0 6px"></div>
      <div id="sr-results"></div>
    </section>`);

  const ask = document.querySelector('nav [data-view="ask"]');
  if (ask) {
    ask.insertAdjacentHTML("afterend", '<button class="nav-item" data-view="search">&#128269; Search</button>');
    document.querySelector('nav [data-view="search"]').addEventListener("click", () => showView("search"));
  }

  function fill(sel, items, label) {
    const cur = sel.value;
    sel.innerHTML = `<option value="">${label}</option>` + items.map(i =>
      `<option value="${E(i.value)}">${E(i.label)}</option>`).join("");
    sel.value = items.some(i => String(i.value) === cur) ? cur : "";
  }

  function facets(f) {
    fill($("sr-dept"), f.departments.map(x => ({ value: x, label: x })), "Any department");
    fill($("sr-tag"), f.tags.map(x => ({ value: x, label: x })), "Any tag");
    fill($("sr-owner"), f.owners.map(o => ({ value: o.id, label: o.name })), "Any owner");
    fill($("sr-vis"), f.visibilities.map(x => ({ value: x, label: x })), "Any visibility");
  }

  async function run() {
    const my = ++seq;
    const p = new URLSearchParams();
    const add = (k, id) => { const v = $(id).value.trim(); if (v) p.set(k, v); };
    add("q", "sr-q"); add("department", "sr-dept"); add("tag", "sr-tag"); add("owner_id", "sr-owner");
    add("visibility", "sr-vis"); add("date_from", "sr-from"); add("date_to", "sr-to");
    p.set("tz_offset", new Date().getTimezoneOffset());      // so a date means your calendar day, not UTC's
    let r;
    try { r = await api("/search?" + p.toString()); }
    catch (e) { if (my === seq) { $("sr-count").textContent = ""; $("sr-results").innerHTML = `<div class="empty-note">Search is unavailable: ${E(e.message)}</div>`; } return; }
    if (my !== seq) return;
    facets(r.facets);
    $("sr-count").textContent = r.query ? `${r.total} result(s) for "${r.query}"` : `${r.total} document(s) you can open`;
    $("sr-results").innerHTML = r.results.length ? r.results.map(d => `
      <div class="doc-row card" style="padding:12px;margin-bottom:8px">
        <a href="#" class="doc-link" data-sr-open="${d.id}"><b>${E(d.title)}</b></a>
        <div style="margin:4px 0"><span class="badge badge-dept">${E(d.department)}</span> <span class="badge badge-internal">${E(d.visibility)}</span>
          ${d.tags.map(t => `<span class="badge badge-file">${E(t)}</span>`).join(" ")}</div>
        <div class="muted">${d.owner ? "Owner: " + E(d.owner) : ""}${d.created_at ? " &middot; " + new Date(d.created_at + (d.created_at.endsWith("Z") ? "" : "Z")).toLocaleDateString() : ""}</div>
        <div style="margin-top:6px">${E(d.snippet)}</div>
      </div>`).join("")
      : `<div class="empty-note">Nothing you are allowed to open matches this search.</div>`;
  }

  const soon = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
  ["sr-q"].forEach(id => $(id).addEventListener("input", soon));
  ["sr-dept", "sr-tag", "sr-owner", "sr-vis", "sr-from", "sr-to"].forEach(id => $(id).addEventListener("change", run));
  $("sr-clear").addEventListener("click", () => {
    ["sr-q", "sr-dept", "sr-tag", "sr-owner", "sr-vis", "sr-from", "sr-to"].forEach(id => { $(id).value = ""; });
    run();
  });
  $("sr-results").addEventListener("click", e => {
    const a = e.target.closest("[data-sr-open]"); if (!a) return;
    e.preventDefault(); showView("documents"); openDoc(Number(a.dataset.srOpen));
  });

  window.FEATURE_VIEWS = Object.assign(window.FEATURE_VIEWS || {}, { search: () => { $("sr-results").innerHTML = ""; $("sr-q").focus(); run(); } });
})();
