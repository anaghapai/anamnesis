// Insights pack UI: criteria-based "Needs review" with reasons, ask-for-review / assign / escalate,
// Review Center, Insights hub (gaps, fix-first queue, impact, what-if, expertise, memory diff),
// last-known-truth, access hints and conflict arbitration hints.
// Hooks into app.js / features.js through: reviewBadge, decorateDocs, decorateViewer, decorateAnswer,
// FEATURE_VIEWS, afterBoot, loadConflicts. Everything lives inside this closure (no global name clashes).
(function () {
  "use strict";
  const A = (p, o) => api(p, o);
  const E = s => esc(s == null ? "" : String(s));
  const stamp = iso => (iso ? new Date(iso + (iso.endsWith("Z") ? "" : "Z")) : null);
  const D = iso => { const d = stamp(iso); return d ? d.toLocaleString() : "—"; };
  const day = iso => { const d = stamp(iso); return d ? d.toLocaleDateString() : "—"; };
  const SEV = { high: "🔴", medium: "🟠", low: "🟡" };
  let META = {}, GAPS = [];

  // ------------------------------------------------------------ small modal ---
  function modal(title, body, onOk, okLabel) {
    const ov = document.createElement("div");
    ov.className = "ia-overlay";
    ov.innerHTML = `<div class="ia-modal card"><h3 style="margin-top:0">${E(title)}</h3><div>${body}</div>
      <div class="auth-error" data-role="err"></div>
      <div class="row-gap"><button class="btn-primary" data-role="ok" style="margin:0">${E(okLabel || "OK")}</button>
      <button class="btn-ghost" data-role="cancel">Cancel</button></div></div>`;
    document.body.appendChild(ov);
    ov.querySelector('[data-role="cancel"]').onclick = () => ov.remove();
    ov.querySelector('[data-role="ok"]').onclick = async () => {
      try { await onOk(ov); ov.remove(); } catch (e) { ov.querySelector('[data-role="err"]').textContent = e.message; }
    };
    const first = ov.querySelector("textarea,input,select"); if (first) first.focus();
    return ov;
  }
  const refreshHere = id => {
    const v = document.querySelector(".view.active");
    if (v && v.id === "view-doc") openDoc(id); else if (v && v.id === "view-review") loadReview(); else loadDocuments();
  };

  // ----------------------------------------------------- badge + reasons ---
  window.reviewBadge = function (d) {
    if (d.needs_review) {
      const n = (d.review_reasons || []).length;
      return `<span class="badge badge-stale ia-sev-${d.review_severity || "low"}" title="${E((d.review_reasons || []).join("\n"))}">⏳ Needs review${n ? ` · ${n} reason${n > 1 ? "s" : ""}` : ""}</span>`;
    }
    return d.verified_until ? `<span class="badge badge-ok">✅ Verified until ${new Date(d.verified_until).toLocaleDateString()}</span>`
                            : `<span class="badge badge-ok">✅ Up to date</span>`;
  };

  function whyHtml(m) {
    const r = m.request;
    const chip = r ? `<div class="ia-chip">🔍 Review requested by <b>${E(r.by)}</b> · ${r.status === "assigned" ? `assigned to <b>${E(r.assignee)}</b>` : `waiting for <b>${E(r.holder || "nobody")}</b>`}
      · due ${D(r.due)}${r.level ? ` · escalated ${r.level}×` : ""}${r.note ? `<div class="muted">“${E(r.note)}”</div>` : ""}</div>` : "";
    if (!m.reasons.length && !chip) return "";
    return `<div class="why-review">${m.reasons.length ? `<b>Why it needs review</b><ul>${m.reasons.map(t => `<li>${E(t)}</li>`).join("")}</ul>` : ""}${chip}</div>`;
  }
  function reviewButtons(id, m) {
    return [
      !m.request ? `<button class="btn-ghost" data-ia="ask" data-id="${id}">🔍 Ask for review</button>` : "",
      m.can_assign && (m.state === "needs_review" || m.request) ? `<button class="btn-ghost" data-ia="assign" data-id="${id}">👤 Assign reviewer</button>` : "",
    ].join(" ");
  }

  const prevDocs = window.decorateDocs;
  window.decorateDocs = async function (docs) {
    if (prevDocs) await prevDocs(docs);
    try { META = await A("/insights/review-meta"); } catch { return; }
    document.querySelectorAll("#doc-list .doc-row").forEach(row => {
      const link = row.querySelector(".doc-link"), mm = link && /openDoc\((\d+)/.exec(link.getAttribute("onclick") || "");
      const id = mm && Number(mm[1]), m = META[id];
      if (!m) return;
      const html = whyHtml(m);
      if (html) row.querySelector(".doc-content").insertAdjacentHTML("afterend", html);
      const bar = row.querySelector(".action-row");
      if (bar) bar.insertAdjacentHTML("beforeend", " " + reviewButtons(id, m));
    });
  };

  const prevViewer = window.decorateViewer;
  window.decorateViewer = async function (d) {
    if (prevViewer) await prevViewer(d);
    try { META = await A("/insights/review-meta"); } catch { return; }
    const m = META[d.id]; if (!m) return;
    const tools = reviewButtons(d.id, m);
    const box = document.createElement("div");
    box.className = "ia-panel";
    box.innerHTML = whyHtml(m) + `<div class="action-row" style="margin:6px 0 0">${tools}</div>`;
    const bar = document.querySelector("#doc-viewer .action-row");
    if (bar) bar.insertAdjacentElement("afterend", box);
  };

  // --------------------------------------------------- answer decorations ---
  const prevAnswer = window.decorateAnswer;
  window.decorateAnswer = function (m, h) {
    if (prevAnswer) h = prevAnswer(m, h);
    let top = "", bottom = "";
    if (m.verified && m.verified.needs_rereview)
      top += `<div class="lkt warn">⚠️ This verified answer is waiting for re-review — ${E(m.verified.rereview_reason || "its source changed")}. Double-check before relying on it.</div>`;
    const l = m.last_known_truth;
    if (l) bottom += `<div class="lkt"><div><b>Current answer:</b> unknown</div>
      <div><b>Last verified:</b> ${E(l.text)} <span class="muted">(${E(l.source)}, as of ${day(l.as_of)}${l.ended ? `, replaced ${day(l.ended)}` : ""})</span></div>
      <div class="muted">Shown as history only. Confirm with the owner before you rely on it.</div></div>`;
    const a = m.access_hint;
    if (a) bottom += `<div class="lkt access" id="ia-acc-${m.qa_id}"><b>🔑 ${a.weak_answer ? "A better answer may exist" : "An answer may exist"} in a document you can't open.</b>
      You won't be shown its content or name. ${a.pending ? "Your request is already waiting for a decision."
      : `You can ask its owner for access. <button class="btn-secondary" data-ia="req-access" data-qa="${m.qa_id}">Request access</button>`}</div>`;
    return top + h + bottom;
  };

  // -------------------------------------------- conflict arbitration hints ---
  const prevConflicts = window.loadConflicts;
  window.loadConflicts = async function () {
    await prevConflicts();
    document.querySelectorAll("#conflict-list .conflict-card").forEach(card => {
      const m = /(?:resolve|propose)Conflict\((\d+),/.exec(card.innerHTML);
      if (!m || card.querySelector(".ia-hint-btn")) return;
      card.insertAdjacentHTML("beforeend", `<div class="ia-hint-row"><button class="btn-ghost ia-hint-btn" data-ia="hint" data-id="${m[1]}">💡 Which looks right?</button><div class="ia-hint-out"></div></div>`);
    });
  };

  // ---------------------------------------------------------- click router ---
  const ACT = {
    ask: id => modal("Ask for a review", `<p class="muted">Goes to the document's owner, who can review it or assign someone. If nobody acts in time it moves up the reporting line.</p>
      <textarea id="ia-note" rows="3" style="width:100%" placeholder="What looks wrong or outdated? (optional)"></textarea>`, async () => {
      const r = await A(`/insights/docs/${id}/ask-review`, { method: "POST", body: JSON.stringify({ note: $("ia-note").value }) });
      alert(r.message || (r.routed_to ? `Sent to ${r.routed_to}.` : "Sent."));
      refreshHere(Number(id));
    }, "Send"),

    assign: async id => {
      let sug = [], people = [];
      try { [sug, people] = await Promise.all([A(`/insights/docs/${id}/reviewer-suggestions`), A("/org/employees")]); } catch (e) { return alert(e.message); }
      const seen = new Set(sug.map(s => s.id));
      const opts = sug.map(s => `<option value="${s.id}">★ ${E(s.name)} (${E(s.role)}, ${E(s.department)}) — ${E(s.why)}</option>`).join("") +
        `<optgroup label="Everyone else">${people.filter(p => !seen.has(p.id) && p.active !== false).map(p => `<option value="${p.id}">${E(p.name)} (${E(p.role)}, ${E(p.department)})</option>`).join("")}</optgroup>`;
      modal("Assign a reviewer", `<p class="muted">Suggestions are ranked from recent real activity on this topic. They must be able to open the document.</p>
        <select id="ia-who" style="width:100%">${opts}</select>
        <textarea id="ia-note" rows="2" style="width:100%;margin-top:8px" placeholder="Note for the reviewer (optional)"></textarea>`, async () => {
        await A(`/insights/docs/${id}/assign-review`, { method: "POST", body: JSON.stringify({ assignee_id: Number($("ia-who").value), note: $("ia-note").value }) });
        refreshHere(Number(id));
      }, "Assign");
    },

    replace: id => modal("Replace the document's text", `<p class="muted">You are vouching for the new text. Verified answers that cited the old text go back for re-review, and people who used it are told.</p>
      <textarea id="ia-content" rows="12" style="width:100%">${E(CUR_DOC && CUR_DOC.id == id ? CUR_DOC.content : "")}</textarea>
      <input type="text" id="ia-note" style="width:100%;margin-top:8px" placeholder="What changed? (optional)">`, async () => {
      const r = await A(`/insights/docs/${id}/replace`, { method: "POST", body: JSON.stringify({ content: $("ia-content").value, note: $("ia-note").value }) });
      alert(`Saved. ${r.answers_marked} verified answer(s) sent back for re-review, ${r.people_notified} people told.`);
      refreshHere(Number(id));
    }, "Replace"),

    "req-access": (_, el) => {
      const qa = el.dataset.qa;
      modal("Request access", `<p class="muted">The owner sees your question and decides. Nothing about the document is shared with you until they approve.</p>
        <select id="ia-days"><option value="1">1 day</option><option value="7">7 days</option><option value="30">30 days</option></select>
        <input type="text" id="ia-note" style="width:100%;margin-top:8px" placeholder="Why do you need it? (optional)">`, async () => {
        const r = await A("/insights/access-request", { method: "POST", body: JSON.stringify({ qa_id: Number(qa), days: Number($("ia-days").value), reason: $("ia-note").value }) });
        const box = $(`ia-acc-${qa}`); if (box) box.innerHTML = `✅ ${E(r.message)}`;
      }, "Send request");
    },

    hint: async (id, el) => {
      const out = el.parentNode.querySelector(".ia-hint-out");
      try {
        const h = await A(`/insights/conflicts/${id}/hint`);
        out.innerHTML = `<div class="lkt"><b>${h.suggest_fact_id ? `Suggestion (${E(h.strength)}): keep “${E(h.sides.find(s => s.fact_id === h.suggest_fact_id).object)}”` : "Too close to call — needs a human."}</b>
          ${h.sides.map(s => `<div class="muted">“${E(s.object)}” — ${s.points} pts: ${s.why.map(E).join("; ")}</div>`).join("")}
          <div class="muted">${E(h.note)}</div></div>`;
      } catch (e) { out.textContent = e.message; }
    },

    open: id => openDoc(Number(id)),
    verify: id => verifyDoc(Number(id)),
    "gap-route": async (i) => { const g = GAPS[i]; try { const r = await A("/insights/gaps/route", { method: "POST", body: JSON.stringify({ question: g.question, department: g.department }) }); alert(r.message); loadInsights(); } catch (e) { alert(e.message); } },
    "gap-answer": i => { const g = GAPS[i];
      modal(`Answer for ${g.department}`, `<p><b>${E(g.question)}</b><br><span class="muted">Asked ${g.count}× by ${g.askers} ${g.askers === 1 ? "person" : "people"} with no verified answer. Your answer is served first from now on, and the askers are told.</span></p>
        <textarea id="ia-ans" rows="4" style="width:100%"></textarea>`, async () => {
        const r = await A("/insights/gaps/answer", { method: "POST", body: JSON.stringify({ question: g.question, department: g.department, answer: $("ia-ans").value }) });
        alert(`Saved. ${r.askers_told} people told.`); loadInsights();
      }, "Save verified answer"); },
    rereview: async (qa, el) => {
      const verdict = el.dataset.v; let corrected = "";
      if (verdict === "correct") { corrected = prompt("Write the corrected answer:"); if (!corrected) return; }
      try { await A(`/insights/rereview/${qa}`, { method: "POST", body: JSON.stringify({ verdict, corrected_answer: corrected || "" }) }); loadReview(); } catch (e) { alert(e.message); }
    },
    "esc-run": async () => { if (!confirm("Run the escalation check now and move overdue items up one level?")) return;
      try { const r = await A("/insights/escalations/run", { method: "POST", body: JSON.stringify({ force: true }) }); alert(`${r.hops.length} item(s) escalated.`); loadReview(); } catch (e) { alert(e.message); } },
    impact: async () => { const id = $("ia-fact").value; if (!id) return;
      const out = $("ia-impact-out"); out.innerHTML = "…";
      try { out.innerHTML = impactHtml(await A(`/insights/impact/fact/${id}`)); } catch (e) { out.textContent = e.message; } },
    whatif: async () => { const out = $("ia-whatif-out"); out.innerHTML = "…";
      try { const r = await A("/insights/what-if", { method: "POST", body: JSON.stringify({ subject: $("wi-s").value, relation: $("wi-r").value, object: $("wi-o").value, text: $("wi-t").value }) });
        out.innerHTML = `<div class="lkt ${r.conflict ? "warn" : ""}">${E(r.note)}${r.would_replace.length ? `<div>Would replace: ${r.would_replace.map(E).join("; ")}</div>` : ""}
          ${r.verified_answers_to_recheck ? `<div>${r.verified_answers_to_recheck} verified answer(s) would need re-review.</div>` : ""}</div>` + impactHtml(r); } catch (e) { out.textContent = e.message; } },
    expert: async () => { const t = $("ex-topic").value.trim(); if (!t) return; const out = $("ex-out");
      try { const r = await A(`/insights/expertise?topic=${encodeURIComponent(t)}`);
        out.innerHTML = r.people.length ? `<table class="mini">${r.people.map(p => `<tr><td>${E(p.name)}</td><td class="muted">${E(p.role)} · ${E(p.department)}</td><td>${p.current ? "✅ current" : "⌛ lapsed"}${p.score != null ? ` (${p.score})` : ""}</td><td class="muted">${E(p.activity)}</td></tr>`).join("")}</table>` : `<div class="muted">Nobody has recorded activity on “${E(r.topic)}”.</div>`; } catch (e) { out.textContent = e.message; } },
    diff: async () => { const out = $("df-out"); out.innerHTML = "…";
      try { const r = await A(`/insights/memory-diff?frm=${$("df-a").value}&to=${$("df-b").value}`);
        const list = (t, rows) => rows.length ? `<details><summary>${t} (${rows.length})</summary><ul>${rows.map(x => `<li>${E(x.text)}${x.at ? ` <span class="muted">${day(x.at)}</span>` : ""}</li>`).join("")}</ul></details>` : "";
        out.innerHTML = `<div class="ia-stats"><div><b>${r.known_then}</b><span>known then</span></div><div><b>${r.known_now}</b><span>known now</span></div>
          <div><b>${r.added.length}</b><span>added</span></div><div><b>${r.superseded.length}</b><span>superseded</span></div>
          <div><b>${r.conflicts_resolved}</b><span>conflicts resolved</span></div><div><b>${r.gaps_opened}</b><span>gaps opened</span></div><div><b>${r.gaps_closed}</b><span>gaps closed</span></div></div>`
          + list("Added", r.added) + list("Superseded", r.superseded); } catch (e) { out.textContent = e.message; } },
  };
  document.addEventListener("click", e => {
    const el = e.target.closest("[data-ia]"); if (!el) return;
    e.preventDefault();
    const fn = ACT[el.dataset.ia]; if (fn) fn(el.dataset.id !== undefined ? el.dataset.id : (el.dataset.i !== undefined ? Number(el.dataset.i) : el.dataset.qa), el);
  });

  // ---------------------------------------------------------- new views ---
  const parent = $("view-health").parentNode;
  parent.insertAdjacentHTML("beforeend", `
    <section id="view-review" class="view"><h2>Review Center</h2></section>
    <section id="view-insights" class="view"><h2>Insights</h2></section>`);
  const healthBtn = document.querySelector('nav [data-view="health"]');
  healthBtn.insertAdjacentHTML("afterend", `
    <button class="nav-item" data-view="review">🔍 Review Center <span class="pill hidden" id="pill-review"></span></button>
    <button class="nav-item" data-view="insights" data-min="manager">🧭 Insights</button>`);
  document.querySelectorAll('nav [data-view="review"], nav [data-view="insights"]').forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
  window.FEATURE_VIEWS = Object.assign(window.FEATURE_VIEWS || {}, { review: () => loadReview(), insights: () => loadInsights() });

  const prevBoot = window.afterBoot;
  window.afterBoot = async function (f) { if (prevBoot) await prevBoot(f); refreshReviewPill(); };
  async function refreshReviewPill() {
    try {
      const q = await A("/insights/review-queue");
      const n = q.docs.filter(d => d.assigned_to_me || d.severity === "high").length + q.rereview.length;
      $("pill-review").textContent = n; $("pill-review").classList.toggle("hidden", !n);
    } catch {}
  }

  const card = (title, sub, inner, id) => `<div class="card" ${id ? `id="${id}"` : ""}><h3 style="margin:0 0 4px">${title}</h3>${sub ? `<p class="view-sub" style="margin:0 0 10px">${sub}</p>` : ""}${inner}</div>`;

  // ------------------------------------------------------- Review Center ---
  async function loadReview() {
    const box = $("view-review");
    box.innerHTML = `<h2>Review Center</h2><p class="view-sub">A document is flagged only when there is a real reason — never just because nobody clicked “verify”.</p><div class="empty-note">Loading…</div>`;
    const mgr = atLeast("manager");
    let q, esc_;
    try { [q, esc_] = await Promise.all([A("/insights/review-queue"), mgr ? A("/insights/escalations") : Promise.resolve(null)]); }
    catch (e) { box.innerHTML += `<div class="denied-note">${E(e.message)}</div>`; return; }
    const mine = q.docs.filter(d => d.assigned_to_me), rest = q.docs.filter(d => !d.assigned_to_me);
    const docRow = (d, mineRow) => `<div class="ia-row"><div><a href="#" data-ia="open" data-id="${d.id}"><b>${E(d.title)}</b></a>
        <span class="badge badge-dept">${E(d.department)}</span> <span class="muted">owner ${E(d.owner || "—")}</span>
        <ul class="ia-reasons">${d.reasons.map(r => `<li>${E(r)}</li>`).join("")}</ul></div>
        <div class="ia-actions">${SEV[d.severity] || ""}
        ${mineRow ? `<button class="btn-primary" style="margin:0" data-ia="verify" data-id="${d.id}">✅ Mark reviewed</button>` : `<button class="btn-ghost" data-ia="ask" data-id="${d.id}">🔍 Ask</button>`}
        ${d.can_assign ? `<button class="btn-ghost" data-ia="assign" data-id="${d.id}">👤 Assign</button>` : ""}</div></div>`;
    const rules = `<details class="card"><summary><b>How “needs review” is decided</b></summary><ul class="ia-reasons">
      <li>🔴 <b>High:</b> an open conflict involves it · it still states a fact that was replaced · a verified answer built on it needs re-review · someone asked for a review</li>
      <li>🟠 <b>Medium:</b> its verification date passed · answers citing it were flagged · its owner is missing/deactivated · not reviewed for 180+ days</li>
      <li>🟡 <b>Low:</b> never verified and not reviewed for 90+ days</li>
      <li>A brand-new upload is <b>not</b> flagged. Anyone can press <b>Ask for review</b>: it goes to the owner, who reviews or assigns someone. If nobody acts within ${esc_ ? esc_.hours : 24}h it moves to the department manager, then an admin.</li></ul></details>`;
    box.innerHTML = `<h2>Review Center</h2><p class="view-sub">A document is flagged only when there is a real reason — never just because nobody clicked “verify”.</p>
      ${mine.length ? card("Assigned to you", "Open the document, check it, then press Mark reviewed.", mine.map(d => docRow(d, true)).join("")) : ""}
      ${card(`Documents that need review (${rest.length})`, "Highest severity first.", rest.length ? rest.map(d => docRow(d, false)).join("") : `<div class="empty-note">Nothing needs review right now. 🎉</div>`)}
      ${mgr ? card(`Verified answers waiting for re-review (${q.rereview.length})`, "Their source changed after a person verified them. They are still served, with a warning, until you decide.",
        q.rereview.length ? q.rereview.map(r => `<div class="ia-row"><div><b>${E(r.question)}</b><div>${E(r.answer)}</div><div class="muted">${E(r.reason)}</div></div>
          <div class="ia-actions"><button class="btn-ghost" data-ia="rereview" data-id="${r.qa_id}" data-v="keep">Still correct</button>
          <button class="btn-ghost" data-ia="rereview" data-id="${r.qa_id}" data-v="correct">Correct it</button>
          <button class="btn-ghost danger" data-ia="rereview" data-id="${r.qa_id}" data-v="retire">Retire</button></div></div>`).join("") : `<div class="empty-note">None.</div>`) : ""}
      ${esc_ ? card("Escalations", `Nothing is allowed to sit unanswered: after ${esc_.hours}h an item moves up the reporting line, and every hop is logged.`,
        (esc_.items.length ? `<table class="mini">${esc_.items.map(i => `<tr><td>${i.kind === "answer" ? "🚩 flagged answer" : "📄 document review"}</td><td>${E(i.title)}</td>
          <td>with <b>${E(i.holder || "—")}</b></td><td class="muted">level ${i.level}${i.chain && i.chain.length ? ` of ${i.chain.length - 1}` : ""} · due ${D(i.due)}</td></tr>`).join("")}</table>` : `<div class="empty-note">No open items.</div>`)
        + (esc_.log.length ? `<h4 class="muted" style="margin:12px 0 4px">Escalation history</h4><ul class="ia-reasons">${esc_.log.map(l => `<li>${D(l.at)} — ${E(l.detail)}</li>`).join("")}</ul>` : "")
        + (atLeast("admin") ? `<button class="btn-ghost" data-ia="esc-run">⏩ Run escalation check now</button>` : "")) : ""}
      ${rules}`;
    refreshReviewPill();
  }

  // ---------------------------------------------------------- Insights hub ---
  function impactHtml(r) {
    return `<div class="ia-impact"><div class="ia-big">${E(r.summary)}</div>
      ${r.teams.length ? `<div>Teams: ${r.teams.map(t => `<span class="badge badge-dept">${E(t)}</span>`).join(" ")}</div>` : ""}
      ${r.people_who_used_it ? `<div class="muted">${r.people_who_used_it} ${r.people_who_used_it === 1 ? "person has" : "people have"} used answers touching this.</div>` : ""}
      ${r.documents.length ? `<details open><summary>Documents (${r.documents.length})</summary><ul>${r.documents.map(d => `<li><a href="#" data-ia="open" data-id="${d.id}">${E(d.title)}</a> <span class="muted">${E(d.department)}</span></li>`).join("")}</ul></details>` : ""}
      ${r.facts.length ? `<details><summary>Connected facts (${r.facts.length})</summary><ul>${r.facts.map(f => `<li>${E(f.text)} <span class="muted">${f.distance === 0 ? "the fact itself" : f.distance + " hop" + (f.distance > 1 ? "s" : "") + " away"}</span></li>`).join("")}</ul></details>` : ""}</div>`;
  }

  async function loadInsights() {
    const box = $("view-insights");
    const today = new Date().toISOString().slice(0, 10), monthAgo = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
    box.innerHTML = `<h2>Insights</h2><p class="view-sub">Where knowledge is missing, what breaks when a fact changes, and who holds it. All plain rules and arithmetic.</p>
      ${card("🔥 Fix first", "Conflicts and gaps ranked by dependent documents × how often people ask.", `<div id="ia-blast" class="empty-note">Loading…</div>`)}
      ${card("🕳️ Knowledge gaps", "Repeated questions with no verified answer, by department. At 3 asks they go to the department manager automatically.", `<div id="ia-gaps" class="empty-note">Loading…</div>`)}
      ${card("🕸️ Dependency impact", "Pick a fact and see what depends on it if it changes.", `<div class="row-gap"><select id="ia-fact" style="flex:1"></select><button class="btn-secondary" data-ia="impact">Show impact</button></div><div id="ia-impact-out"></div>`)}
      ${card("🔮 What-if policy preview", "Draft a change and see who it touches before you publish. Nothing is saved.", `<div class="row-gap"><input id="wi-s" placeholder="Subject (e.g. Postgres Migration)"><input id="wi-r" placeholder="Relation (e.g. deadline)"><input id="wi-o" placeholder="New value"></div>
        <textarea id="wi-t" rows="2" style="width:100%;margin-top:8px" placeholder="…or paste the draft policy text"></textarea><button class="btn-secondary" data-ia="whatif">Preview</button><div id="ia-whatif-out"></div>`)}
      ${card("🚌 Knowledge bus factor", "Topics that depend on one person's current expertise. Scores come from real uploads, reviews and approvals, halving every 90 days.", `<div id="ia-bus" class="empty-note">Loading…</div>
        <div class="row-gap"><input id="ex-topic" placeholder="Look up experts for a topic (e.g. vpn)"><button class="btn-secondary" data-ia="expert">Find</button></div><div id="ex-out"></div>`)}
      ${card("⏱️ Organizational memory diff", "What the organization knew on one date versus another.", `<div class="row-gap"><input type="date" id="df-a" value="${monthAgo}"><input type="date" id="df-b" value="${today}"><button class="btn-secondary" data-ia="diff">Compare</button></div><div id="df-out"></div>`)}`;
    A("/insights/blast-radius").then(rows => {
      $("ia-blast").className = "";
      $("ia-blast").innerHTML = rows.length ? `<table class="mini"><tr class="muted"><td></td><td>What</td><td>Documents</td><td>Asked</td><td>Score</td></tr>${rows.map((r, i) => `<tr><td>${i + 1}. ${r.kind === "conflict" ? "⚠️ conflict" : "🕳️ gap"}</td><td>${E(r.title)}</td><td>${r.documents}</td><td>${r.asks}×</td><td><b>${r.score}</b></td></tr>`).join("")}</table>` : `<div class="empty-note">Nothing to fix. 🎉</div>`;
    }).catch(e => { $("ia-blast").textContent = e.message; });
    A("/insights/gaps").then(rows => {
      GAPS = rows; $("ia-gaps").className = "";
      $("ia-gaps").innerHTML = rows.length ? `<table class="mini">${rows.map((g, i) => `<tr><td><b>${E(g.question)}</b><div class="muted">${E(g.department)} · asked ${g.count}× by ${g.askers} ${g.askers === 1 ? "person" : "people"}</div></td>
        <td>${g.answered ? "✅ answered" : g.routed ? `📨 with ${E(g.routed_to || "—")}` : "open"}</td>
        <td>${g.answered ? "" : `${g.routed ? "" : `<button class="btn-ghost" data-ia="gap-route" data-i="${i}">Send to manager</button>`} <button class="btn-secondary" data-ia="gap-answer" data-i="${i}">Write answer</button>`}</td></tr>`).join("")}</table>`
        : `<div class="empty-note">No repeated unanswered questions yet.</div>`;
    }).catch(e => { $("ia-gaps").textContent = e.message; });
    A("/insights/fact-list").then(rows => fillSelect($("ia-fact"), rows.map(f => ({ value: f.id, label: `${f.text}${f.status === "superseded" ? " (superseded)" : ""}` })))).catch(() => {});
    A("/insights/bus-factor").then(rows => {
      $("ia-bus").className = "";
      $("ia-bus").innerHTML = rows.length ? `<table class="mini">${rows.map(r => `<tr><td>${r.risk === "none" ? "🔴" : "🟠"} <b>${E(r.topic)}</b></td><td>${E(r.detail)}</td></tr>`).join("")}</table>` : `<div class="empty-note">Every topic has at least two current experts.</div>`;
    }).catch(e => { $("ia-bus").textContent = e.message; });
  }
})();
