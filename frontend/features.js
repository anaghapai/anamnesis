// Feature pack 2 - UI for: workspace + approvals, recycle bin, health score, confidence,
// tags / favorites / comments / related, per-document access, one-time links, dual control, help bot.
// Hooks into app.js through: FEATURE_VIEWS, afterBoot, decorateDocs, decorateViewer, decorateAnswer.

const fdate = iso => (iso ? new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleString() : "—");
const safe = fn => async (...a) => { try { return await fn(...a); } catch (e) { alert(e.message); } };
let DOC_META = {};

// ------------------------------------------------------------- view router ---
window.FEATURE_VIEWS = {
  workspace: () => loadWorkspace(),
  approvals: () => loadApprovals(),
  health: () => loadHealth(),
  recycle: () => loadRecycle(),
};

window.afterBoot = async function (forcePw) {
  refreshApprovalPill();
  if (forcePw) return;
  const q = new URLSearchParams(location.search);
  const jit = q.get("jit"), doc = q.get("doc");
  if (!jit && !doc) return;
  history.replaceState(null, "", location.pathname);
  if (jit) {
    try {
      const r = await api("/jit/redeem", { method: "POST", body: JSON.stringify({ token: jit }) });
      alert(`One-time link accepted. You can open "${r.title}" until ${fdate(r.expires_at)}.`);
      openDoc(r.document_id);
    } catch (e) { alert(e.message); }
  } else {
    try { await api(`/documents/${doc}`); openDoc(Number(doc)); }
    catch {
      showView("documents"); $("dacc-id").value = doc;
      msg("dacc-msg", `You can't open document #${doc}. Fill in a reason and press Request to ask for access.`);
      $("dacc-id").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
};

async function refreshApprovalPill() {
  if (!atLeast("manager")) return;
  try {
    const a = await api("/approvals");
    const n = a.documents.length + a.dual_control.filter(x => x.can_decide).length;
    $("pill-approvals").textContent = n; $("pill-approvals").classList.toggle("hidden", !n);
  } catch {}
}

// ------------------------------------------------------------ confidence ---
window.decorateAnswer = function (m, h) {
  const c = m.confidence;
  if (!c) return h;
  const box = `<div class="conf conf-${c.level}"><span class="conf-dot"></span><b>${c.level} confidence</b>
    <a href="#" onclick="this.parentNode.querySelector('ul').classList.toggle('hidden');return false">why?</a>
    <ul class="hidden">${c.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></div>`;
  const i = h.indexOf('<div class="action-row">');
  return i >= 0 ? h.slice(0, i) + box + h.slice(i) : h + box;
};

// -------------------------------------------------------- document list ---
window.decorateDocs = async function (docs) {
  loadDocAccess().catch(() => {});
  try { DOC_META = await api("/documents-meta"); } catch { DOC_META = {}; }
  const rows = [...document.querySelectorAll("#doc-list .doc-row")];
  const decorated = rows.map((row, i) => ({ row, d: docs[i] })).filter(x => x.d);
  decorated.forEach(({ row, d }) => {
    const m = DOC_META[d.id] || { tags: [], pinned: false, comments: 0 };
    const top = row.querySelector(".doc-row-top");
    const star = `<a href="#" class="star ${m.pinned ? "on" : ""}" title="Pin / unpin" onclick="toggleStar(${d.id});return false">${m.pinned ? "★" : "☆"}</a> `;
    top.insertAdjacentHTML("afterbegin", star);
    const tags = m.tags.map(t => `<span class="tag">#${esc(t)}</span>`).join(" ");
    const extra = `<div class="tag-row">${tags} <span class="muted">Doc #${d.id}${m.comments ? ` · 💬 ${m.comments}` : ""}</span></div>`;
    row.querySelector(".doc-content").insertAdjacentHTML("afterend", extra);
    if (m.pinned) row.classList.add("pinned");
  });
  decorated.filter(x => (DOC_META[x.d.id] || {}).pinned).reverse().forEach(x => x.row.parentNode.prepend(x.row));
};
window.toggleStar = safe(async id => { await api(`/documents/${id}/favorite`, { method: "POST" }); loadDocuments(); });

// ------------------------------------------------------- document viewer ---
window.decorateViewer = async function (d) {
  let x;
  try { x = await api(`/documents/${d.id}/extras`); } catch { return; }
  const tags = x.tags.map(t => `<span class="tag">#${esc(t)}</span>`).join(" ") || `<span class="muted">no tags</span>`;
  const tools = [
    `<button class="btn-ghost" onclick="toggleStar(${d.id}).then(()=>openDoc(${d.id}))">${x.pinned ? "★ Unpin" : "☆ Pin as favorite"}</button>`,
    `<button class="btn-ghost" onclick="copyLink(${d.id})">🔗 Copy link</button>`,
    x.can_see_access ? `<button class="btn-ghost" onclick="showAccess(${d.id})">👥 Who has access</button>` : "",
    x.can_manage && ["confidential", "restricted"].includes(d.visibility) ? `<button class="btn-ghost" onclick="showViews(${d.id})">👁️ View history</button>` : "",
    x.can_manage ? `<button class="btn-ghost" onclick="editTags(${d.id}, '${esc(x.tags.join(", ")).replace(/'/g, "")}')">🏷️ Edit tags</button>` : "",
    x.can_jit ? `<button class="btn-ghost" onclick="makeJit(${d.id})">⏱️ One-time link</button>` : "",
    x.can_delete ? `<button class="btn-ghost danger" onclick="deleteDoc(${d.id})">🗑️ Delete</button>` : "",
  ].join(" ");
  const admin = x.can_classify ? `<details class="card"><summary>🛡️ Classification &amp; permanent access</summary>
      <div class="row-gap"><select id="cls-sel">${["public", "internal", "confidential", "restricted"].map(v => `<option ${v === d.visibility ? "selected" : ""}>${v}</option>`).join("")}</select>
        <button class="btn-secondary" onclick="applyClass(${d.id})">Change classification</button></div>
      ${d.visibility === "restricted" ? `<p class="muted">Loosening a Restricted document, or adding a permanent reader, needs a second approver (dual control).</p>
      <div class="row-gap"><select id="perm-sel"></select><button class="btn-secondary" onclick="givePermanent(${d.id})">Request permanent access</button></div>` : ""}
      <div class="auth-error" id="cls-msg"></div></details>` : "";
  const rel = x.related.length ? x.related.map(r => `<a class="rel-link" href="#" onclick="openDoc(${r.id});return false">${esc(r.title)}</a> <span class="muted">via ${esc(r.via.join(", "))}</span>`).join("<br>") : `<span class="muted">Nothing connected yet - add facts to the knowledge graph or tag documents.</span>`;
  const comments = x.comments.map(c => `<div class="comment"><b>${esc(c.user)}</b> <span class="muted">${fdate(c.at)}</span>
      ${c.can_delete ? `<a href="#" class="muted" onclick="delComment(${c.id}, ${d.id});return false">delete</a>` : ""}<div>${esc(c.text)}</div></div>`).join("");
  const rejectNote = x.review_note ? `<div class="hint-card">📝 Reviewer note: ${esc(x.review_note)}</div>` : "";
  const panel = document.createElement("div");
  panel.className = "extras";
  panel.innerHTML = `${rejectNote}<div class="tag-row">${tags} <span class="muted">· Doc #${d.id}</span></div>
    <div class="action-row" style="margin:8px 0">${tools}</div>${admin}
    <div id="extras-out"></div>
    <h4>Related documents</h4><div>${rel}</div>
    ${x.workspace === "company" ? `<h4>Comments</h4>${comments || '<span class="muted">No comments yet.</span>'}
    <div class="row-gap"><input type="text" id="cmt-in" placeholder="Add a comment" maxlength="2000"><button class="btn-secondary" onclick="addComment(${d.id})">Post</button></div>` : ""}`;
  $("doc-viewer").appendChild(panel);
  if ($("perm-sel")) {
    try { const people = await api("/org/employees"); fillSelect($("perm-sel"), people.map(p => ({ value: p.id, label: `${p.name} (${p.department})` }))); } catch {}
  }
};
window.copyLink = id => { const u = `${location.origin}/?doc=${id}`; (navigator.clipboard ? navigator.clipboard.writeText(u) : Promise.reject()).then(() => alert("Link copied. People without access will see a Request access form."), () => prompt("Copy this link:", u)); };
window.editTags = safe(async (id, cur) => {
  const v = prompt("Tags, comma separated:", cur); if (v === null) return;
  await api(`/documents/${id}/tags`, { method: "PUT", body: JSON.stringify({ tags: v.split(",") }) }); openDoc(id);
});
window.addComment = safe(async id => {
  const t = $("cmt-in").value.trim(); if (!t) return;
  await api(`/documents/${id}/comments`, { method: "POST", body: JSON.stringify({ text: t }) }); openDoc(id);
});
window.delComment = safe(async (cid, id) => { await api(`/comments/${cid}`, { method: "DELETE" }); openDoc(id); });
window.deleteDoc = safe(async id => {
  if (!confirm("Move this document to the Recycle Bin? You can restore it for 30 days.")) return;
  await api(`/documents/${id}`, { method: "DELETE" }); showView("recycle");
});
window.showAccess = safe(async id => {
  const r = await api(`/documents/${id}/access`);
  $("extras-out").innerHTML = `<div class="card"><b>${r.people.length} people can open this (${esc(r.visibility)}, ${esc(r.department)})</b>
    <table class="mini">${r.people.map(p => `<tr><td>${esc(p.name)}</td><td class="muted">${esc(p.role)} · ${esc(p.department)}</td><td>${esc(p.why)}</td></tr>`).join("")}</table></div>`;
});
window.showViews = safe(async id => {
  const r = await api(`/documents/${id}/views`);
  $("extras-out").innerHTML = `<div class="card"><b>Who opened this document</b>${r.length ? `<table class="mini">${r.map(v => `<tr><td>${esc(v.user)}</td><td class="muted">${fdate(v.at)}</td></tr>`).join("")}</table>` : `<div class="muted">Nobody yet.</div>`}</div>`;
});
window.makeJit = safe(async id => {
  const m = parseInt(prompt("Link valid for how many minutes? (15 - 60)", "30"), 10); if (!m) return;
  const r = await api(`/documents/${id}/jit`, { method: "POST", body: JSON.stringify({ minutes: m }) });
  const url = location.origin + r.path;
  $("extras-out").innerHTML = `<div class="card"><b>One-time link (valid ${r.minutes} min, works once, for one person)</b>
    <input type="text" readonly value="${esc(url)}" onclick="this.select()" style="width:100%;margin-top:8px"><div class="muted">Expires ${fdate(r.expires_at)}. The person must log in first; opening it is logged.</div></div>`;
});
window.applyClass = safe(async id => {
  const r = await api(`/documents/${id}/classify`, { method: "POST", body: JSON.stringify({ visibility: $("cls-sel").value }) });
  if (r.applied) openDoc(id); else msg("cls-msg", r.message, true);
});
window.givePermanent = safe(async id => {
  const r = await api(`/documents/${id}/permanent-access`, { method: "POST", body: JSON.stringify({ user_id: Number($("perm-sel").value) }) });
  msg("cls-msg", r.message, true);
});

// ------------------------------------------------ per-document access (Documents page) ---
$("dacc-btn").addEventListener("click", safe(async () => {
  msg("dacc-msg", "");
  const id = Number($("dacc-id").value); if (!id) return msg("dacc-msg", "Enter the document number");
  const r = await api("/doc-access-requests", { method: "POST", body: JSON.stringify({ document_id: id, reason: $("dacc-reason").value, days: Number($("dacc-days").value) }) });
  msg("dacc-msg", r.message, true); loadDocAccess();
}));
async function loadDocAccess() {
  const r = await api("/doc-access-requests");
  $("dacc-mine").innerHTML = r.mine.slice(0, 6).map(a => `<div class="doc-row"><b>${esc(a.title)}</b> <span class="badge badge-${a.status === "approved" ? "ok" : a.status === "denied" ? "flagged" : "stale"}">${a.status}</span>
    <span class="muted">${a.days}-day request · ${fdate(a.created_at)}</span>${a.status === "approved" ? ` <a href="#" onclick="openDoc(${a.document_id});return false">open</a>` : ""}</div>`).join("");
  $("dacc-review-wrap").classList.toggle("hidden", !r.for_review.length);
  $("dacc-review").innerHTML = r.for_review.map(a => `<div class="doc-row"><b>${esc(a.requester)}</b> wants <b>${a.days} day(s)</b> on <a href="#" onclick="openDoc(${a.document_id});return false">${esc(a.title)}</a>
    <div class="muted">${esc(a.reason || "no reason given")}</div><div class="action-row">
    <button class="btn-primary" style="margin:0" onclick="decideDocAccess(${a.id},true,${a.days})">Approve</button>
    <button class="btn-ghost" onclick="decideDocAccess(${a.id},false)">Deny</button></div></div>`).join("");
}
window.decideDocAccess = safe(async (id, approve, days) => {
  await api(`/doc-access-requests/${id}/decide`, { method: "POST", body: JSON.stringify({ approve, days }) }); loadDocAccess();
});

// ------------------------------------------------------------- workspace ---
document.querySelectorAll("[data-wstab]").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll("[data-wstab]").forEach(x => x.classList.toggle("active", x === t));
  $("ws-file-wrap").classList.toggle("hidden", t.dataset.wstab !== "file");
  $("ws-text-wrap").classList.toggle("hidden", t.dataset.wstab !== "text");
}));
$("ws-add-btn").addEventListener("click", async () => {
  msg("ws-msg", "");
  try {
    if (!$("ws-file-wrap").classList.contains("hidden")) {
      const f = $("ws-file").files[0]; if (!f) throw new Error("Choose a file first");
      const fd = new FormData(); fd.append("file", f); fd.append("title", $("ws-title").value);
      await api("/workspace/upload", { method: "POST", body: fd });
    } else {
      await api("/workspace/documents", { method: "POST", body: JSON.stringify({ title: $("ws-title").value || "Untitled", content: $("ws-content").value }) });
    }
    ["ws-file", "ws-content", "ws-title"].forEach(i => $(i).value = ""); loadWorkspace();
  } catch (e) { msg("ws-msg", e.message); }
});
const WS_BADGE = { private: ["🔒 Private", "restricted"], pending: ["⏳ Waiting for approval", "stale"], changes: ["✏️ Changes requested", "confidential"], rejected: ["✖ Rejected", "flagged"] };
async function loadWorkspace() {
  const w = await api("/workspace");
  $("ws-docs").innerHTML = w.documents.map(d => {
    const [label, cls] = WS_BADGE[d.status];
    const buttons = d.status === "pending"
      ? `<button class="btn-ghost" onclick="wsWithdraw(${d.id})">↩ Withdraw</button>`
      : `<button class="btn-primary" style="margin:0" onclick="wsSubmitForm(${d.id})">📤 Submit for approval</button>
         <button class="btn-ghost" onclick="wsEdit(${d.id})">✏️ Edit</button>`;
    return `<div class="doc-row"><div class="doc-row-top"><a class="doc-title doc-link" href="#" onclick="openDoc(${d.id});return false">${esc(d.title)}</a>
      <span><span class="badge badge-file">${esc(d.file_type)}</span> <span class="badge badge-${cls}">${label}</span></span></div>
      <div class="doc-content">${esc(d.preview)}</div>
      ${d.review_note ? `<div class="hint-card">📝 ${esc(d.review_note)}</div>` : ""}
      ${d.status === "pending" ? `<div class="muted">Sent to ${esc(d.submit_department)} as ${esc(d.submit_visibility)} on ${fdate(d.submitted_at)}</div>` : ""}
      <div class="action-row">${buttons}<button class="btn-ghost danger" onclick="deleteDoc(${d.id})">🗑️ Delete</button></div>
      <div id="ws-form-${d.id}"></div></div>`;
  }).join("") || `<div class="empty-note">Nothing here yet. Upload a file or paste text above - it stays private.</div>`;
  $("ws-notes").innerHTML = w.notes.map(noteHtml).join("") || `<div class="empty-note">No notes yet.</div>`;
}
window.wsSubmitForm = id => {
  const depts = ["All", ...ME.departments];
  $(`ws-form-${id}`).innerHTML = `<div class="card"><div class="row-gap">
    <select id="sub-dept-${id}">${depts.map(x => `<option ${x === ME.department ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
    <select id="sub-vis-${id}"><option>public</option><option selected>internal</option><option>confidential</option><option>restricted</option></select></div>
    <input type="text" id="sub-note-${id}" placeholder="Note to the approver (optional)">
    <div class="row-gap"><button class="btn-primary" onclick="wsSubmit(${id})">Send for approval</button>
    <button class="btn-ghost" onclick="$('ws-form-${id}').innerHTML=''">Cancel</button></div></div>`;
};
window.wsSubmit = safe(async id => {
  const r = await api(`/workspace/documents/${id}/submit`, { method: "POST", body: JSON.stringify({
    department: $(`sub-dept-${id}`).value, visibility: $(`sub-vis-${id}`).value, note: $(`sub-note-${id}`).value }) });
  alert(`Submitted. Waiting on: ${r.waiting_on.join(", ") || "the owner"}.`); loadWorkspace();
});
window.wsWithdraw = safe(async id => { await api(`/workspace/documents/${id}/withdraw`, { method: "POST" }); loadWorkspace(); });
window.wsEdit = safe(async id => {
  const d = await api(`/documents/${id}`);
  $(`ws-form-${id}`).innerHTML = `<div class="card"><input type="text" id="ed-t-${id}" value="${esc(d.title)}">
    <textarea id="ed-c-${id}" rows="8">${esc(d.content)}</textarea><div class="row-gap">
    <button class="btn-primary" onclick="wsSaveEdit(${id})">Save</button><button class="btn-ghost" onclick="$('ws-form-${id}').innerHTML=''">Cancel</button></div></div>`;
});
window.wsSaveEdit = safe(async id => {
  await api(`/workspace/documents/${id}`, { method: "PATCH", body: JSON.stringify({ title: $(`ed-t-${id}`).value, content: $(`ed-c-${id}`).value }) }); loadWorkspace();
});

// notes + checklists (private)
const NOTES = {};
function noteHtml(n) {
  NOTES[n.id] = n;
  if (n.kind === "checklist") {
    let items = []; try { items = JSON.parse(n.body || "[]"); } catch {}
    return `<div class="card note"><b>☑ ${esc(n.title)}</b>${items.map((it, i) => `<label class="ck"><input type="checkbox" ${it.done ? "checked" : ""} onchange="toggleItem(${n.id},${i},this.checked)"> <span class="${it.done ? "done" : ""}">${esc(it.t)}</span></label>`).join("")}
      <div class="row-gap"><input type="text" id="ck-new-${n.id}" placeholder="Add item"><button class="btn-secondary" onclick="addItem(${n.id})">+</button></div>
      <a href="#" class="muted" onclick="delNote(${n.id});return false">delete</a></div>`;
  }
  return `<div class="card note"><b>📝 ${esc(n.title)}</b><textarea id="nb-${n.id}" rows="4">${esc(n.body)}</textarea>
    <div class="row-gap"><button class="btn-secondary" onclick="saveNote(${n.id})">Save</button><a href="#" class="muted" onclick="delNote(${n.id});return false">delete</a></div></div>`;
}
window.newNote = safe(async kind => {
  const title = prompt(kind === "checklist" ? "Checklist title:" : "Note title:"); if (!title) return;
  await api("/notes", { method: "POST", body: JSON.stringify({ kind, title, body: kind === "checklist" ? "[]" : "" }) }); loadWorkspace();
});
window.saveNote = safe(async id => { const n = NOTES[id]; await api(`/notes/${id}`, { method: "PUT", body: JSON.stringify({ kind: n.kind, title: n.title, body: $(`nb-${id}`).value }) }); loadWorkspace(); });
window.delNote = safe(async id => { if (confirm("Delete this?")) { await api(`/notes/${id}`, { method: "DELETE" }); loadWorkspace(); } });
async function putChecklist(id, items) { const n = NOTES[id]; await api(`/notes/${id}`, { method: "PUT", body: JSON.stringify({ kind: n.kind, title: n.title, body: JSON.stringify(items) }) }); loadWorkspace(); }
window.toggleItem = safe(async (id, i, done) => { const it = JSON.parse(NOTES[id].body || "[]"); it[i].done = done; await putChecklist(id, it); });
window.addItem = safe(async id => { const t = $(`ck-new-${id}`).value.trim(); if (!t) return; const it = JSON.parse(NOTES[id].body || "[]"); it.push({ t, done: false }); await putChecklist(id, it); });

// ------------------------------------------------------------- approvals ---
async function loadApprovals() {
  const a = await api("/approvals");
  $("appr-docs").innerHTML = a.documents.map(d => `<div class="doc-row"><div class="doc-row-top"><b>${esc(d.title)}</b>
    <span><span class="badge badge-dept">${esc(d.department)}</span> <span class="badge badge-${d.visibility}">${d.visibility}</span></span></div>
    <div class="muted">From ${esc(d.author)} · ${fdate(d.submitted_at)}${d.note ? " · “" + esc(d.note) + "”" : ""}</div>
    <div class="doc-content">${esc(d.preview)}</div>
    <div class="action-row"><button class="btn-secondary" onclick="apprRead(${d.id})">📖 Read full text</button>
    <button class="btn-primary" style="margin:0" onclick="apprDecide(${d.id},'approve')">Approve</button>
    <button class="btn-ghost" onclick="apprDecide(${d.id},'changes')">Request changes</button>
    <button class="btn-ghost danger" onclick="apprDecide(${d.id},'reject')">Reject</button></div><div id="appr-read-${d.id}"></div></div>`).join("")
    || `<div class="empty-note">Nothing waiting for approval.</div>`;
  $("appr-dual").innerHTML = a.dual_control.map(x => `<div class="doc-row"><b>${esc(x.action)}</b> on <a href="#" onclick="openDoc(${x.document_id});return false">${esc(x.title)}</a>
    <div class="muted">Requested by ${esc(x.requested_by)} · ${fdate(x.created_at)} · needs a second person</div>
    ${x.can_decide ? `<div class="action-row"><button class="btn-primary" style="margin:0" onclick="dualDecide(${x.id},true)">Approve</button><button class="btn-ghost" onclick="dualDecide(${x.id},false)">Reject</button></div>` : `<div class="muted">You made this request - another manager, admin or owner must decide.</div>`}</div>`).join("")
    || `<div class="empty-note">No pending dual-control requests.</div>`;
  refreshApprovalPill();
}
window.apprRead = safe(async id => {
  const box = $(`appr-read-${id}`); if (box.innerHTML) { box.innerHTML = ""; return; }
  const d = await api(`/approvals/${id}`); box.innerHTML = `<div class="doc-fulltext">${esc(d.content)}</div>`;
});
window.apprDecide = safe(async (id, decision) => {
  let note = "";
  if (decision !== "approve") { note = prompt(decision === "reject" ? "Why are you rejecting it?" : "What should be changed?"); if (!note) return; }
  await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ decision, note }) }); loadApprovals();
});
window.dualDecide = safe(async (id, approve) => { await api(`/dual-control/${id}/decide`, { method: "POST", body: JSON.stringify({ approve }) }); loadApprovals(); });

// ---------------------------------------------------------------- health ---
async function loadHealth() {
  const h = await api("/health-score");
  const col = h.score >= 80 ? "var(--success)" : h.score >= 55 ? "var(--warn)" : "var(--danger)";
  $("health-box").innerHTML = `<div class="health-top"><div class="ring" style="--p:${h.score};--c:${col}"><span>${h.score}</span></div>
    <div><h3 style="margin:0;color:${col}">${h.grade}</h3><div class="muted">Score = ${esc(h.formula)}</div></div></div>
    ${h.parts.map(p => `<div class="card part"><div class="row-between"><b>${esc(p.label)}</b><span>${p.points} / ${p.max}</span></div>
      <div class="bar"><div style="width:${(p.points / p.max) * 100}%;background:${col}"></div></div><div class="muted">${esc(p.detail)}</div></div>`).join("")}
    <h3>Most overdue for review</h3>${h.needs_review.map(d => `<div class="doc-row"><a href="#" onclick="openDoc(${d.id});return false">${esc(d.title)}</a>
      <span class="muted">last reviewed: ${d.last_reviewed_at ? fdate(d.last_reviewed_at) : "never"}</span></div>`).join("") || '<div class="empty-note">Everything you can see was reviewed in the last 90 days. 🎉</div>'}`;
}

// ---------------------------------------------------------- recycle bin ---
async function loadRecycle() {
  const items = await api("/recycle-bin");
  $("recycle-list").innerHTML = items.map(d => `<div class="doc-row"><div class="doc-row-top"><b>${esc(d.title)}</b>
    <span><span class="badge badge-dept">${esc(d.department)}</span> <span class="badge badge-stale">${d.days_left} days left</span></span></div>
    <div class="muted">Deleted ${fdate(d.deleted_at)}${d.deleted_by ? " by " + esc(d.deleted_by) : ""}</div>
    <div class="action-row"><button class="btn-primary" style="margin:0" onclick="restoreDoc(${d.id})">↩ Restore</button>
    ${atLeast("admin") ? `<button class="btn-ghost danger" onclick="purgeDoc(${d.id})">Delete forever</button>` : ""}</div></div>`).join("")
    || `<div class="empty-note">The recycle bin is empty.</div>`;
}
window.restoreDoc = safe(async id => { await api(`/recycle-bin/${id}/restore`, { method: "POST" }); loadRecycle(); });
window.purgeDoc = safe(async id => { if (confirm("Delete permanently? This cannot be undone.")) { await api(`/recycle-bin/${id}`, { method: "DELETE" }); loadRecycle(); } });

// ------------------------------------------------------------ help chatbot ---
$("help-fab").addEventListener("click", () => {
  $("help-panel").classList.toggle("hidden");
  if (!$("help-log").children.length) helpSay("bot", "Hi! Ask me how to use Anamnesis - e.g. <i>How do I request access?</i>, <i>What does Restricted mean?</i>, <i>Where is the Conflicts page?</i>");
});
$("help-close").addEventListener("click", e => { e.preventDefault(); $("help-panel").classList.add("hidden"); });
function helpSay(who, html) { const d = document.createElement("div"); d.className = "hmsg " + who; d.innerHTML = html; $("help-log").appendChild(d); $("help-log").scrollTop = 1e6; }
const md = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>");
$("help-form").addEventListener("submit", async e => {
  e.preventDefault();
  const q = $("help-input").value.trim(); if (!q) return; $("help-input").value = "";
  helpSay("me", esc(q));
  try {
    const r = await api("/help/ask", { method: "POST", body: JSON.stringify({ question: q }) });
    helpSay("bot", md(r.answer) + (r.go_to ? ` <a href="#" onclick="showView('${r.go_to}');return false">Take me there →</a>` : ""));
  } catch (err) { helpSay("bot", esc(err.message)); }
});
