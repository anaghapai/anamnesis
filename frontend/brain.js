// Brain pack UI: answer-state badge + "why?" chain, Ask the Organization (authorized experts only),
// Knowledge Impact view (edit a document -> see what it touches), version history, related questions,
// Starting Point, optional local-model explain. Everything lives in this closure (no global name clashes).
// Hooks: decorateAnswer, decorateViewer, FEATURE_VIEWS, afterBoot.
(function () {
  "use strict";
  const A = (p, o) => api(p, o);
  const E = s => esc(s == null ? "" : String(s));
  const stamp = iso => (iso ? new Date(iso + (iso.endsWith("Z") ? "" : "Z")) : null);
  const D = iso => { const d = stamp(iso); return d ? d.toLocaleString() : "—"; };
  const ICON = { current: "🟢", verified: "✅", stale: "⏳", needs_review: "🔄", conflicting: "⚠️", weak: "🟡", no_knowledge: "🕳️" };
  const POST = b => ({ method: "POST", body: JSON.stringify(b) });

  // ------------------------------------------------------------------ modal ---
  function modal(title, body, buttons) {
    const ov = document.createElement("div");
    ov.className = "ia-overlay";
    ov.innerHTML = `<div class="ia-modal card"><h3 style="margin-top:0">${E(title)}</h3><div data-role="body">${body}</div>
      <div class="auth-error" data-role="err"></div><div class="row-gap" data-role="btns"></div></div>`;
    document.body.appendChild(ov);
    const bar = ov.querySelector('[data-role="btns"]');
    (buttons || []).concat([{ label: "Close", cls: "btn-ghost", fn: null }]).forEach(b => {
      const el = document.createElement("button");
      el.className = b.cls || "btn-primary"; el.textContent = b.label; el.style.margin = "0";
      el.onclick = async () => {
        if (!b.fn) return ov.remove();
        try { await b.fn(ov); } catch (e) { ov.querySelector('[data-role="err"]').textContent = e.message; }
      };
      bar.appendChild(el);
    });
    return ov;
  }
  const lab = x => (typeof x === "string" ? x : x.text || x.question || x.title || x.name || x.label || JSON.stringify(x));

  function renderAnalysis(a) {
    if (!a) return "";
    const c = a.counts || {};
    let h = `<div class="bn-counts">${[["changes", "changed sentence(s)"], ["answers", "answer(s)"], ["verified_answers", "verified"],
      ["facts", "fact(s)"], ["documents", "related document(s)"], ["people", "person(s) told"]]
      .map(([k, l]) => `<span class="bn-chip ${c[k] ? "on" : ""}"><b>${c[k] || 0}</b> ${l}</span>`).join("")}</div>`;
    (a.changes || []).forEach(x => { h += `<div class="bn-diff"><del>${E(x.old)}</del><ins>${E(x.new)}</ins></div>`; });
    [["answers", "Answers built on the old wording"], ["facts", "Graph facts"], ["documents", "Related documents"], ["people", "People who received the old information"]]
      .forEach(([k, t]) => { if ((a[k] || []).length) h += `<h4 class="bn-h">${t}</h4><ul class="bn-list">${a[k].map(x => `<li>${E(lab(x))}${x.answers ? ` <span class="muted">(${x.answers})</span>` : ""}</li>`).join("")}</ul>`; });
    if ((a.recommended_actions || []).length) h += `<h4 class="bn-h">Suggested next steps</h4><ul class="bn-list">${a.recommended_actions.map(t => `<li>${E(t)}</li>`).join("")}</ul>`;
    return h;
  }

  // ---------------------------------------------------- answer decorations ---
  const prevAnswer = window.decorateAnswer;
  window.decorateAnswer = function (m, h) {
    if (prevAnswer) h = prevAnswer(m, h);
    const s = m.state;
    const top = s ? `<div class="bn-state bn-${E(s.code)}"><span>${ICON[s.code] || "•"} <b>${E(s.label)}</b></span>
      <span class="muted">${E(s.detail)}</span>${s.code === "needs_review" ? ` <a href="#" data-bn="why" data-qa="${m.qa_id}">why?</a>` : ""}</div>` : "";
    const weak = !m.answered || (s && ["no_knowledge", "weak"].includes(s.code)) || (m.confidence && m.confidence.level === "Low");
    const btns = [];
    if (weak) btns.push(`<button class="btn-secondary" data-bn="ask" data-qa="${m.qa_id}">🙋 Ask the organization</button>`);
    btns.push(`<button class="btn-ghost" data-bn="related" data-qa="${m.qa_id}">💡 Related questions</button>`);
    if (m.answer) btns.push(`<button class="btn-ghost" data-bn="explain" data-qa="${m.qa_id}">🤖 Explain (local model)</button>`);
    return top + h + `<div class="action-row bn-actions">${btns.join(" ")}</div><div id="bn-out-${m.qa_id}"></div>`;
  };

  const ACT = {
    async why(qa) {
      const r = await A(`/impact/qa/${qa}/why`);
      if (!r.affected) return modal("Why?", E(r.message), []);
      const steps = r.steps.map(s => {
        const rows = [];
        if (s.title) rows.push(`${E(s.title)}${s.version ? ` → v${s.version}` : ""}`);
        if (s.old || s.new) rows.push(`<div class="bn-diff"><del>${E(s.old)}</del><ins>${E(s.new)}</ins></div>`);
        if (s.text) rows.push(`“${E(s.text)}”`);
        if (s.reason) rows.push(E(s.reason));
        const label = s.label || { change: "What changed", passage: "The passage the answer used" }[s.kind] || s.kind;
        return `<div class="bn-step"><b>${E(label)}</b>${rows.map(x => `<div>${x}</div>`).join("")}</div>`;
      }).join('<div class="bn-arrow">↓</div>');
      const btns = r.can_revalidate ? [{ label: "Open Knowledge Impact", cls: "btn-secondary", fn: ov => { ov.remove(); showView("impact"); } }] : [];
      modal("Why is this answer flagged?", steps, btns);
    },
    async ask(qa) {
      const m = MSGS[qa]; if (!m) return;
      const r = await A("/knowledge/experts", POST({ question: m.question, qa_id: qa }));
      const send = async (ov, id, name) => {
        const x = await A("/knowledge/requests", POST({ question: m.question, qa_id: qa, assignee_id: id }));
        ov.querySelector('[data-role="body"]').innerHTML = `<p>${x.duplicate ? "You already asked this — still waiting on" : "Sent to"} <b>${E(x.assigned_to || name)}</b>.
          They'll see the question only (no documents are attached). Watch <a href="#" onclick="showView('requests');return false">Ask the Organization</a> for the reply.</p>`;
        ov.querySelector('[data-role="btns"]').querySelectorAll("button:not(:last-child)").forEach(b => b.remove());
        refreshPills();
      };
      const body = `<p class="muted">Only people who are authorized for this knowledge area are suggested — the system never guesses access.
        Topic: <b>${E(r.topic)}</b></p>` +
        (r.experts.length ? r.experts.map(e => `<div class="bn-expert"><div><b>${E(e.name)}</b> <span class="muted">${E(e.role)} · ${E(e.department)}</span></div>
          <div class="muted">${E(e.why)}</div><button class="btn-secondary" data-pick="${e.id}" data-name="${E(e.name)}">Ask ${E(e.name)}</button></div>`).join("")
          : `<p>${E(r.message || "No expert could be suggested.")}</p>`) +
        (r.fallback ? `<div class="bn-expert"><div>Or send it to your supervisor: <b>${E(r.fallback.name)}</b></div>
          <button class="btn-ghost" data-pick="" data-name="${E(r.fallback.name)}">Send to ${E(r.fallback.name)}</button></div>` : "");
      const ov = modal("Ask the organization", body, []);
      ov.querySelectorAll("[data-pick]").forEach(b => b.onclick = async () => {
        try { await send(ov, b.dataset.pick ? Number(b.dataset.pick) : null, b.dataset.name); } catch (e) { ov.querySelector('[data-role="err"]').textContent = e.message; }
      });
    },
    async related(qa) {
      const r = await A(`/knowledge/related/${qa}`);
      const box = $(`bn-out-${qa}`); if (!box) return;
      const qs = r.questions || [];
      box.innerHTML = qs.length ? `<div class="bn-related"><b>💡 People also asked</b>${qs.map(q => `<a href="#" data-bn="use-q" data-q="${E(lab(q))}">${E(lab(q))}</a>`).join("")}</div>`
        : `<div class="muted bn-related">No related questions yet.</div>`;
    },
    "use-q"(_, el) { const i = $("ask-input"); i.value = el.dataset.q; i.focus(); },
    async explain(qa) {
      const box = $(`bn-out-${qa}`); box.innerHTML = `<div class="muted bn-related">Asking the local model…</div>`;
      try {
        const r = await A(`/ask/${qa}/explain`, { method: "POST" });
        box.innerHTML = `<div class="bn-related">${r.abstained ? `<b>🛑 ${E(r.message)}</b>` : `<b>🤖 ${E(r.model)} (runs locally)</b><div>${E(r.answer)}</div>`}
          ${r.removed && r.removed.length ? `<div class="muted">${r.removed.length} claim(s) removed — not supported by the evidence.</div>` : ""}
          <details><summary>Evidence given to the model</summary>${(r.evidence || []).map(e => `<div class="muted">[${E(e.id)}] ${E(e.text)}</div>`).join("")}</details></div>`;
      } catch (e) { box.innerHTML = `<div class="bn-related muted">${E(e.message)}</div>`; }
    },
  };
  document.addEventListener("click", e => {
    const el = e.target.closest("[data-bn]"); if (!el) return;
    e.preventDefault();
    const fn = ACT[el.dataset.bn]; if (fn) Promise.resolve().then(() => fn(el.dataset.qa, el)).catch(err => alert(err.message));
  });

  // --------------------------------------------------------- document viewer ---
  const prevViewer = window.decorateViewer;
  window.decorateViewer = async function (d) {
    if (prevViewer) await prevViewer(d);
    let x; try { x = await A(`/documents/${d.id}/impact`); } catch { return; }
    const last = x.last_change;
    const panel = document.createElement("div");
    panel.className = "bn-panel";
    panel.innerHTML = `<h4 class="bn-h">🔄 Knowledge impact</h4>
      <div class="muted">${x.answer_count} answer(s) were built on this document${x.verified_count ? `, ${x.verified_count} human-verified` : ""}${x.flagged_count ? `, <b>${x.flagged_count} need review</b>` : ""}.
      ${last ? `Last change: v${last.version} by ${E(last.by)} on ${D(last.at)}.` : "No managed edits yet."}</div>
      <div class="action-row" style="margin:6px 0 0">${x.can_edit ? `<button class="btn-secondary" data-id="${d.id}" id="bn-edit">✏️ Edit text (preview impact first)</button>` : ""}
      <button class="btn-ghost" id="bn-versions">🕘 Versions (${x.versions})</button></div>`;
    const bar = document.querySelector("#doc-viewer .action-row");
    if (bar) bar.insertAdjacentElement("afterend", panel);
    const ed = panel.querySelector("#bn-edit"); if (ed) ed.onclick = () => editDoc(d);
    panel.querySelector("#bn-versions").onclick = () => versions(d.id);
  };

  async function editDoc(d) {
    const cur = (CUR_DOC && CUR_DOC.id === d.id) ? CUR_DOC.content : (await A(`/documents/${d.id}`)).content;
    const ov = modal(`Edit “${d.title}”`, `<textarea id="bn-text" rows="10" style="width:100%">${E(cur)}</textarea>
      <input type="text" id="bn-note" placeholder="What changed? (optional)" style="width:100%;margin-top:6px"><div id="bn-prev" class="bn-prev"></div>`, [
      { label: "Preview impact", cls: "btn-secondary", fn: async o => {
        const r = await A(`/documents/${d.id}/content`, { method: "PUT", body: JSON.stringify({ content: o.querySelector("#bn-text").value, preview: true }) });
        o.querySelector("#bn-prev").innerHTML = r.analysis ? renderAnalysis(r.analysis) : E(r.message);
      } },
      { label: "Apply change", fn: async o => {
        const r = await A(`/documents/${d.id}/content`, { method: "PUT", body: JSON.stringify({ content: o.querySelector("#bn-text").value, note: o.querySelector("#bn-note").value }) });
        if (!r.applied) throw new Error(r.message || "Nothing changed");
        o.remove(); openDoc(d.id);
        modal(`Saved as v${r.version}`, renderAnalysis(r.analysis), [{ label: "Open Knowledge Impact", cls: "btn-secondary", fn: m => { m.remove(); showView("impact"); } }]);
        refreshPills();
      } }]);
    ov.querySelector(".ia-modal").style.width = "min(760px,94vw)";
  }

  async function versions(id) {
    const r = await A(`/documents/${id}/versions`);
    const ov = modal("Version history", r.versions.slice().reverse().map(v => `<div class="bn-expert"><b>v${v.version}</b>${v.current ? " (current)" : ""}
      <span class="muted">${E(v.by)} · ${D(v.at)} · ${E(v.note)}</span>${v.version > 1 ? ` <a href="#" data-v="${v.version}">what changed?</a>` : ""}</div>`).join("") + `<div id="bn-cmp"></div>`, []);
    ov.querySelectorAll("[data-v]").forEach(a => a.onclick = async e => {
      e.preventDefault(); const v = Number(a.dataset.v);
      const c = await A(`/documents/${id}/compare?a=${v - 1}&b=${v}`);
      ov.querySelector("#bn-cmp").innerHTML = `<h4 class="bn-h">v${v - 1} → v${v}</h4>` + ((c.changes || []).map(x => `<div class="bn-diff"><del>${E(x.old)}</del><ins>${E(x.new)}</ins></div>`).join("") || "<div class='muted'>No sentence-level changes.</div>");
    });
  }

  // ------------------------------------------------------------------- views ---
  const parent = $("view-health").parentNode;
  parent.insertAdjacentHTML("beforeend", `
    <section id="view-impact" class="view"><h2>Knowledge Impact</h2></section>
    <section id="view-requests" class="view"><h2>Ask the Organization</h2></section>
    <section id="view-start" class="view"><h2>Starting Point</h2></section>`);
  const anchor = document.querySelector('nav [data-view="insights"]') || document.querySelector('nav [data-view="health"]');
  anchor.insertAdjacentHTML("afterend", `
    <button class="nav-item" data-view="impact">🔄 Knowledge Impact <span class="pill hidden" id="pill-impact"></span></button>
    <button class="nav-item" data-view="requests">🙋 Ask the Organization <span class="pill hidden" id="pill-requests"></span></button>
    <button class="nav-item" data-view="start">🧭 Starting Point</button>`);
  document.querySelectorAll('nav [data-view="impact"], nav [data-view="requests"], nav [data-view="start"]')
    .forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
  window.FEATURE_VIEWS = Object.assign(window.FEATURE_VIEWS || {}, { impact: () => loadImpact(), requests: () => loadRequests(), start: () => loadStart() });

  async function refreshPills() {
    const set = (id, n) => { const e = $(id); if (e) { e.textContent = n; e.classList.toggle("hidden", !n); } };
    try { const o = await A("/impact/overview"); set("pill-impact", o.mine.length + (o.is_manager ? o.queue.length : 0)); } catch {}
    try { const r = await A("/knowledge/requests"); set("pill-requests", r.assigned.length + r.verify.length); } catch {}
  }
  const prevBoot = window.afterBoot;
  window.afterBoot = async function (f) { if (prevBoot) await prevBoot(f); refreshPills(); };

  const card = (t, sub, inner) => `<div class="card"><h3 style="margin:0 0 4px">${t}</h3>${sub ? `<p class="view-sub" style="margin:0 0 10px">${sub}</p>` : ""}${inner}</div>`;
  const item = (q, extra) => `<div class="bn-item"><div><b>${E(q.question)}</b> <span class="badge badge-${q.status === "verified" ? "verified" : "stale"}">${E(q.status)}</span></div>
    <div class="muted">${E(q.reason)}</div><div class="bn-old">Answer given: ${E(q.answer)}</div>${extra || ""}</div>`;

  async function loadImpact() {
    const o = await A("/impact/overview"), v = $("view-impact");
    let h = `<p class="view-sub">When a document or fact changes, Anamnesis finds the answers built on the OLD wording and marks them <b>needs review</b> — nothing is deleted, and the asker is told.</p>`;
    h += card("Answers you received that need a second look", "Ask again to get the current wording.",
      o.mine.length ? o.mine.map(q => item(q, `<a href="#" data-bn="why" data-qa="${q.qa_id}">why was this flagged?</a>`)).join("") : "<div class='empty-note'>Nothing you relied on has changed. ✅</div>");
    if (o.is_manager) {
      h += card("Revalidation queue", "Decide: still valid, or replace it with the correct answer.",
        o.queue.length ? o.queue.map(q => item(q, `<div class="action-row"><button class="btn-primary" data-rv="still_valid" data-id="${q.qa_id}">Still valid</button>
          <button class="btn-secondary" data-rv="replace" data-id="${q.qa_id}">Replace answer</button>
          <a href="#" data-bn="why" data-qa="${q.qa_id}">why?</a></div>`)).join("") : "<div class='empty-note'>Queue is clear.</div>");
      h += card("Recent changes", "Click one to see exactly what it touched.",
        o.events.length ? o.events.map(e => `<div class="bn-item"><a href="#" data-ev="${e.id}"><b>${E(e.title)}</b></a>
          <span class="muted"> by ${E(e.by)} · ${D(e.at)}</span><div>${["answers", "verified_answers", "facts", "documents", "people"].map(k => `<span class="bn-chip ${e.counts[k] ? "on" : ""}"><b>${e.counts[k] || 0}</b> ${k.replace("_", " ")}</span>`).join("")}</div></div>`).join("")
          : "<div class='empty-note'>No changes tracked yet. Open a document and use “Edit text”.</div>");
    }
    v.innerHTML = `<h2>Knowledge Impact</h2>` + h;
    v.querySelectorAll("[data-ev]").forEach(a => a.onclick = async e => {
      e.preventDefault(); const r = await A(`/impact/events/${a.dataset.ev}`);
      modal("What this change touched", renderAnalysis(r), []);
    });
    v.querySelectorAll("[data-rv]").forEach(b => b.onclick = () => revalidate(Number(b.dataset.id), b.dataset.rv));
    refreshPills();
  }

  function revalidate(id, action) {
    const replace = action === "replace";
    modal(replace ? "Replace the answer" : "Mark still valid",
      `${replace ? `<textarea id="bn-new" rows="3" style="width:100%" placeholder="The correct answer"></textarea>` : ""}<input type="text" id="bn-rn" placeholder="Note (optional)" style="width:100%;margin-top:6px">`,
      [{ label: "Confirm", fn: async o => {
        await A(`/impact/qa/${id}/revalidate`, POST({ action, note: o.querySelector("#bn-rn").value || null, new_answer: replace ? o.querySelector("#bn-new").value : null }));
        o.remove(); loadImpact();
      } }]);
  }

  async function loadRequests() {
    const r = await A("/knowledge/requests"), v = $("view-requests");
    const row = (x, acts) => `<div class="bn-item"><div><b>${E(x.question)}</b> <span class="badge badge-stale">${E(x.status.replace("_", " "))}</span></div>
      <div class="muted">From ${E(x.requester)} → ${E(x.assigned_to || "—")} (${E(x.route)}${x.reason ? ": " + E(x.reason) : ""}) · ${D(x.created_at)}</div>
      ${x.answer ? `<div class="bn-old">Answer: ${E(x.answer)}${x.answered_by ? ` — ${E(x.answered_by)}` : ""}${x.verified_by ? `, verified by ${E(x.verified_by)}` : ""}</div>` : ""}${acts || ""}</div>`;
    let h = `<p class="view-sub">Questions nobody's documents could answer, sent to a person who is authorized for that knowledge area. An answer only becomes organizational knowledge after a second authorized person verifies it.</p>`;
    h += card("Waiting for your answer", "", r.assigned.length ? r.assigned.map(x => row(x, `<div class="action-row"><button class="btn-primary" data-k="answer" data-id="${x.id}">Answer</button>
      <button class="btn-ghost" data-k="forward" data-id="${x.id}">Forward</button><button class="btn-ghost" data-k="decline" data-id="${x.id}">Decline</button></div>`)).join("") : "<div class='empty-note'>Nothing waiting on you.</div>");
    if (r.verify.length) h += card("Waiting for your verification", "", r.verify.map(x => row(x, `<div class="action-row"><button class="btn-primary" data-k="approve" data-id="${x.id}">Verify &amp; publish</button>
      <button class="btn-ghost" data-k="reject" data-id="${x.id}">Reject</button></div>`)).join(""));
    h += card("Questions you asked", "", r.mine.length ? r.mine.map(x => row(x)).join("") : "<div class='empty-note'>You haven't sent any. Use “Ask the organization” under an unanswered question.</div>");
    if (r.handled.length) h += card("Handled by you", "", r.handled.map(x => row(x)).join(""));
    v.innerHTML = `<h2>Ask the Organization</h2>` + h;
    v.querySelectorAll("[data-k]").forEach(b => b.onclick = () => reqAction(Number(b.dataset.id), b.dataset.k));
    refreshPills();
  }

  async function reqAction(id, k) {
    const done = o => { o.remove(); loadRequests(); };
    const share = `<select id="bn-sw" style="margin-top:6px"><option value="department">Share with my department</option><option value="company">Share company-wide</option></select>`;
    if (k === "answer") return modal("Answer from what you know", `<textarea id="bn-a" rows="4" style="width:100%"></textarea>${share}
      <label class="ck"><input type="checkbox" id="bn-v" checked> Send for verification so it becomes organizational knowledge</label>`,
      [{ label: "Send answer", fn: async o => { await A(`/knowledge/requests/${id}/answer`, POST({ answer: o.querySelector("#bn-a").value, verify: o.querySelector("#bn-v").checked, share_with: o.querySelector("#bn-sw").value })); done(o); } }]);
    if (k === "decline") return modal("Decline", `<input type="text" id="bn-n" placeholder="Reason (optional)" style="width:100%">`,
      [{ label: "Decline", fn: async o => { await A(`/knowledge/requests/${id}/decline`, POST({ note: o.querySelector("#bn-n").value })); done(o); } }]);
    if (k === "forward") {
      const r = await A(`/knowledge/requests/${id}/forward-options`);
      return modal("Forward to someone authorized", r.options.length ? `<select id="bn-to" style="width:100%">${r.options.map(p => `<option value="${p.id}">${E(p.name)} (${E(p.role)}, ${E(p.department)})</option>`).join("")}</select>
        <input type="text" id="bn-n" placeholder="Note (optional)" style="width:100%;margin-top:6px">` : "<p>Nobody else is authorized for this knowledge area.</p>",
        r.options.length ? [{ label: "Forward", fn: async o => { await A(`/knowledge/requests/${id}/forward`, POST({ to_user_id: Number(o.querySelector("#bn-to").value), note: o.querySelector("#bn-n").value })); done(o); } }] : []);
    }
    return modal(k === "approve" ? "Verify & publish" : "Reject", `<input type="text" id="bn-n" placeholder="Note (optional)" style="width:100%">${k === "approve" ? share : ""}`,
      [{ label: "Confirm", fn: async o => { await A(`/knowledge/requests/${id}/verify`, POST({ approve: k === "approve", note: o.querySelector("#bn-n").value, share_with: k === "approve" ? o.querySelector("#bn-sw").value : "department" })); done(o); } }]);
  }

  async function loadStart() {
    const o = await A("/onboarding"), v = $("view-start");
    const list = (t, arr, fn) => arr && arr.length ? card(t, "", `<ul class="bn-list">${arr.map(fn).join("")}</ul>`) : "";
    v.innerHTML = `<h2>Starting Point</h2><p class="view-sub">${E(o.name)} · ${E(o.role)} · ${E(o.department)}${o.supervisor ? ` · reports to ${E(o.supervisor)}` : ""}</p>
      ${(o.topics || []).length ? `<div>${o.topics.map(t => `<span class="tag">#${E(t)}</span>`).join(" ")}</div>` : ""}
      ${list("📚 Read first", o.documents, d => `<li><a href="#" onclick="openDoc(${d.id});return false">${E(d.title)}</a> <span class="muted">${E(d.why)}</span></li>`)}
      ${list("🙋 People to know", o.experts, p => `<li><b>${E(p.name)}</b> <span class="muted">${E(p.role)} · ${E(p.department)} — ${E(p.why)}</span></li>`)}
      ${list("💬 Good first questions", o.questions, q => `<li><a href="#" data-bn="goask" data-q="${E(lab(q))}">${E(lab(q))}</a></li>`)}`;
    v.querySelectorAll('[data-bn="goask"]').forEach(a => a.onclick = e => { e.preventDefault(); showView("ask"); const i = $("ask-input"); i.value = a.dataset.q; i.focus(); });
  }
})();
