const RANK = { guest: 0, intern: 1, member: 2, manager: 3, admin: 4, owner: 5 };
const ROLES = ["guest", "intern", "member", "manager", "admin", "owner"];
let TOKEN = localStorage.getItem("anamnesis_token") || null;
let ME = null;
let network = null;
const $ = id => document.getElementById(id);

// ---------------------------------------------------------------- utils ---
async function api(path, options = {}) {
  const headers = options.headers || {};
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401 && TOKEN) { logout(); throw new Error("Session expired — please log in again"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
  return data;
}
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function msg(id, text, ok = false) { const e = $(id); e.style.color = ok ? "var(--success)" : "var(--danger)"; e.textContent = text; }
const atLeast = role => RANK[ME.role] >= RANK[role];
const fmt = iso => new Date(iso + "Z").toLocaleString();
function fillSelect(el, items, blank) {
  el.innerHTML = (blank ? `<option value="">${blank}</option>` : "") +
    items.map(i => `<option value="${esc(i.value ?? i)}">${esc(i.label ?? i)}</option>`).join("");
}
function applyAccent(c) { document.documentElement.style.setProperty("--cyan", c || "#22d3ee"); }

// ------------------------------------------------------------ auth flow ---
document.querySelectorAll(".auth-tab[data-tab]").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".auth-tab[data-tab]").forEach(t => t.classList.toggle("active", t === tab));
  $("login-form").classList.toggle("hidden", tab.dataset.tab !== "login");
  $("signup-form").classList.toggle("hidden", tab.dataset.tab !== "signup");
}));

$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); msg("login-error", "");
  try {
    const d = await api("/auth/login", { method: "POST", body: JSON.stringify({
      identifier: $("login-id").value, password: $("login-password").value }) });
    TOKEN = d.access_token; localStorage.setItem("anamnesis_token", TOKEN);
    boot(d.must_change_password);
  } catch (err) { msg("login-error", err.message); }
});

$("signup-form").addEventListener("submit", async e => {
  e.preventDefault(); msg("signup-error", "");
  try {
    const d = await api("/auth/signup", { method: "POST", body: JSON.stringify({
      name: $("signup-name").value, email: $("signup-email").value,
      org_name: $("signup-orgname").value, password: $("signup-password").value }) });
    TOKEN = d.access_token; localStorage.setItem("anamnesis_token", TOKEN);
    boot();
  } catch (err) { msg("signup-error", err.message); }
});

function logout() {
  TOKEN = null; ME = null; localStorage.removeItem("anamnesis_token");
  applyAccent(null);
  $("app").classList.add("hidden"); $("auth-screen").classList.remove("hidden");
}
$("logout-btn").addEventListener("click", logout);

// ------------------------------------------------------------ nav / boot ---
document.querySelectorAll(".nav-item").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));

function showView(name) {
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  ({ dashboard: loadDashboard, mywork: loadMyWork, ask: initAsk,
     documents: () => { loadDocuments(); loadAccessRequests(); },
     graph: loadGraph, conflicts: loadConflicts, reviews: loadReviews, people: loadPeople,
     settings: loadSettings, audit: loadAudit, account: loadAccount, ...(window.FEATURE_VIEWS || {}) }[name] || (() => {}))();
}

async function boot(forcePw) {
  try { ME = await api("/me"); } catch { logout(); return; }
  $("auth-screen").classList.add("hidden"); $("app").classList.remove("hidden");
  $("me-name").textContent = ME.name;
  $("me-role").textContent = `${ME.role} · ${ME.department}`;
  $("me-org").textContent = ME.org_name;
  applyAccent(ME.accent);
  document.querySelectorAll("[data-min]").forEach(el => {
    el.style.display = atLeast(el.dataset.min) ? "" : "none";
  });
  fillSelect($("doc-filter"), ME.departments, "All departments");
  fillSelect($("doc-dept"), ["All", ...ME.departments]);
  refreshPills();
  if (forcePw || ME.must_change_password) showView("account"); else showView("dashboard");
  if (window.afterBoot) window.afterBoot(forcePw || ME.must_change_password);
}

async function refreshPills() {
  try {
    const w = await api("/my-work");
    const set = (id, n) => { $(id).textContent = n; $(id).classList.toggle("hidden", !n); };
    set("pill-work", w.unread_updates); set("pill-reviews", w.pending_reviews);
  } catch {}
}

// ----------------------------------------------------------- dashboard ---
async function loadDashboard() {
  const s = await api("/dashboard");
  const card = (n, l, view) => `<div class="stat-card" style="cursor:pointer" onclick="showView('${view}')"><div class="stat-num">${n}</div><div class="stat-label">${l}</div></div>`;
  $("stat-grid").innerHTML = card(s.documents, "Documents", "documents") + card(s.active_facts, "Active facts", "graph") +
    card(s.open_conflicts, "Open conflicts", "conflicts") + card(s.open_tasks, "Open team tasks", "mywork") +
    card(s.questions, "Questions asked", "ask") + card(s.verified_answers, "Human-verified answers", "reviews") +
    card(s.pending_reviews, "Awaiting review", "reviews") + card(s.people, "People", "people");
}

// ------------------------------------------------------------------ chat ---
// One chat = one thread of questions. Every question after the first is a follow-up:
// the backend folds the earlier turns in as context, so "what about contractors?" just works.
const MSGS = {};                              // qa_id -> message data
let CHAT = { root: null, last: null, scope: null };
const STALE_DAYS = 90;

function initAsk() {
  loadChats();
  if (!$("chat-thread").children.length) newChat(CHAT.scope);
}

async function loadChats() {
  try {
    const list = await api("/chats");
    $("chat-list").innerHTML = list.length ? list.map(t => `
      <div class="chat-item ${t.id === CHAT.root ? "active" : ""}" onclick="openChat(${t.id})">
        <div class="chat-item-title">${esc(t.title)}</div>
        <div class="chat-item-meta">${t.count} message${t.count === 1 ? "" : "s"} · ${esc(fmt(t.last_at))}${t.flagged ? " · 🚩" : ""}</div>
      </div>`).join("") : "<div class='empty-note' style='padding:10px'>Your chats will appear here.</div>";
  } catch {}
}

function renderScope() {
  const el = $("chat-scope");
  el.classList.toggle("hidden", !CHAT.scope);
  if (CHAT.scope) el.innerHTML = `Asking only inside <strong>${esc(CHAT.scope.title)}</strong> — <a href="#" onclick="clearScope();return false">ask everything instead</a>`;
  $("ask-input").placeholder = CHAT.scope ? "Ask something about this document…" : "Ask anything, e.g. how many days off do I get?";
}
function clearScope() { CHAT.scope = null; renderScope(); }

function newChat(scope) {
  CHAT = { root: null, last: null, scope: scope || null };
  $("chat-thread").innerHTML = `<div class="chat-intro">
    <div style="font-size:28px">💬</div><strong>What do you want to know?</strong>
    <div class="muted" style="margin-top:6px">Ask in your own words — you don't have to match the document's wording.
    I only search documents you're allowed to open, and I show you the exact passage and where it came from.</div></div>`;
  renderScope(); loadChats(); $("ask-input").focus();
}

async function openChat(root) {
  try {
    const t = await api(`/chats/${root}`);
    CHAT = { root, last: t.messages.length ? t.messages[t.messages.length - 1].qa_id : null, scope: null };
    $("chat-thread").innerHTML = "";
    t.messages.forEach(m => { MSGS[m.qa_id] = m; addTurn(m); });
    renderScope(); loadChats(); scrollChat();
  } catch (err) { alert(err.message); }
}
function scrollChat() { const t = $("chat-thread"); t.scrollTop = t.scrollHeight; }

function addTurn(m) {
  const div = document.createElement("div");
  div.className = "chat-turn"; div.dataset.qa = m.qa_id;
  div.innerHTML = `<div class="bubble-user">${esc(m.question)}</div><div class="bubble-bot card">${answerHtml(m)}</div>`;
  $("chat-thread").appendChild(div);
  if (m.documents && m.documents.length) selectDocTab(m.qa_id, 0);
  return div;
}

function highlightPassage(p) {
  const bits = [];
  if (p.before) bits.push(`<span class="ctx">${esc(p.before)}</span>`);
  bits.push(`<mark>${esc(p.match || p.text)}</mark>`);
  if (p.after) bits.push(`<span class="ctx">${esc(p.after)}</span>`);
  return bits.join(" ");
}

function answerHtml(m) {
  let h = "";
  if (m.verified) {
    const v = m.verified;
    h += `<div class="verified-block"><h4 style="margin:0 0 6px">✅ ${v.kind === "corrected" ? "Corrected" : "Verified"} answer</h4>
      ${esc(v.answer)}<div class="by">Reviewed by ${esc(v.reviewer || "a supervisor")}${v.note ? " — " + esc(v.note) : ""}</div></div>`;
  }
  if (m.answer) {
    h += `<div class="answer-main">${esc(m.answer.text)}</div>
      <div class="answer-src">From <a href="#" onclick="openDocFromMsg(${m.qa_id},${m.answer.document_id});return false">${esc(m.answer.document_title)}</a>
      · <a href="#" onclick="openDocFromMsg(${m.qa_id},${m.answer.document_id});return false">open full document ↗</a></div>`;
  } else if (!m.verified && !m.multi_hop) {
    h += m.answer_plain
      ? `<div class="answer-main">${esc(m.answer_plain)}</div>`
      : `<div class="denied-note">🔒 I couldn't find that in any document you're allowed to open. Try different words — or it may exist but be restricted to someone else.</div>`;
  }
  if (m.multi_hop) h += `<div class="result-block"><h4>🕸️ Connected facts</h4><div class="hop-chain">${esc(m.multi_hop.path)}</div></div>`;
  const docs = m.documents || [];
  if (docs.length) {
    h += `<div class="result-block"><h4>📄 ${docs.length === 1 ? "Source" : `Found in ${docs.length} documents — switch tabs`}</h4>
      <div class="doc-tabs">${docs.map((g, i) => `<button class="doc-tab" id="tab-${m.qa_id}-${i}" onclick="selectDocTab(${m.qa_id},${i})">${esc(g.title)}</button>`).join("")}</div>
      <div class="doc-pane" id="pane-${m.qa_id}"></div></div>`;
  }
  const answered = m.answered && (m.answer || m.verified || m.answer_plain);
  if (answered) {
    const flaggable = !m.status || m.status === "unreviewed" || m.status === "rejected";
    h += `<div class="action-row">
      ${flaggable ? `<button class="btn-secondary" onclick="flagAnswer(${m.qa_id})">🚩 Flag as wrong / outdated</button>` : `<span class="badge badge-${m.status === "flagged" ? "flagged" : "verified"}">${esc(m.status)}</span>`}
      <button class="btn-ghost" onclick="usedFor(${m.qa_id})">📌 I used this for a task</button></div>
      <div class="auth-error" id="ask-msg-${m.qa_id}"></div>`;
  }
  return window.decorateAnswer ? window.decorateAnswer(m, h) : h;
}

function selectDocTab(qaId, i) {
  const m = MSGS[qaId], pane = $(`pane-${qaId}`);
  if (!m || !pane) return;
  const g = m.documents[i];
  document.querySelectorAll(`[id^="tab-${qaId}-"]`).forEach((b, j) => b.classList.toggle("active", j === i));
  const stale = g.age_days > STALE_DAYS ? `<span class="badge badge-stale">⏳ ${g.age_days} days old — may be outdated</span>` : "";
  pane.innerHTML = `<div class="pane-meta"><span class="badge badge-dept">${esc(g.department)}</span> ${stale}
      <button class="btn-ghost" style="margin-left:auto" onclick="openDocFromMsg(${qaId},${g.document_id})">Open full document ↗</button></div>` +
    g.passages.map(p => `<div class="passage">${p.heading ? `<div class="passage-h">${esc(p.heading)}</div>` : ""}${highlightPassage(p)}</div>`).join("");
}

function openDocFromMsg(qaId, docId) {
  const m = MSGS[qaId], g = m && (m.documents || []).find(x => x.document_id === docId);
  const hl = m && m.answer && m.answer.document_id === docId ? m.answer.text : (g && g.passages[0] ? g.passages[0].match : null);
  openDoc(docId, hl);
}

$("chat-new-btn").addEventListener("click", () => newChat(null));

$("ask-form").addEventListener("submit", async e => {
  e.preventDefault();
  const input = $("ask-input"), question = input.value.trim();
  if (!question) return;
  const thread = $("chat-thread");
  const intro = thread.querySelector(".chat-intro"); if (intro) intro.remove();
  input.value = "";
  const turn = document.createElement("div");
  turn.className = "chat-turn";
  turn.innerHTML = `<div class="bubble-user">${esc(question)}</div><div class="bubble-bot card"><div class="empty-note" style="padding:6px">Searching documents you can access…</div></div>`;
  thread.appendChild(turn); scrollChat();
  try {
    const d = await api("/ask", { method: "POST", body: JSON.stringify({
      question, follow_up_to: CHAT.last, document_id: CHAT.scope ? CHAT.scope.id : null }) });
    if (!CHAT.root) CHAT.root = d.qa_id;
    CHAT.last = d.qa_id; MSGS[d.qa_id] = d; turn.dataset.qa = d.qa_id;
    turn.querySelector(".bubble-bot").innerHTML = answerHtml(d);
    if (d.documents.length) selectDocTab(d.qa_id, 0);
    loadChats(); scrollChat();
  } catch (err) { turn.querySelector(".bubble-bot").innerHTML = `<div class="denied-note">${esc(err.message)}</div>`; }
  input.focus();
});

async function flagAnswer(id) {
  const note = prompt("What looks wrong or outdated? (optional)");
  if (note === null) return;
  try {
    const r = await api(`/qa/${id}/flag`, { method: "POST", body: JSON.stringify({ note }) });
    msg(`ask-msg-${id}`, `Sent to ${r.routed_to || "a reviewer"} for review.`, true);
    if (MSGS[id]) MSGS[id].status = "flagged";
    loadChats();
  } catch (err) { msg(`ask-msg-${id}`, err.message); }
}

async function usedFor(id) {
  const text = prompt("What did you use this for? (e.g. 'Used for the vendor onboarding checklist')");
  if (!text) return;
  const m = MSGS[id], top = m && (m.answer ? { document_id: m.answer.document_id } : (m.passages || [])[0]);
  try {
    const r = await api("/updates", { method: "POST", body: JSON.stringify({
      text, kind: "used_for", document_id: top ? top.document_id : null, qa_id: id }) });
    msg(`ask-msg-${id}`, `Note sent to ${r.sent_to} ${r.sent_to === 1 ? "person" : "people"}.`, true);
  } catch (err) { msg(`ask-msg-${id}`, err.message); }
}

// ------------------------------------------------------------ documents ---
$("new-doc-btn").addEventListener("click", async () => {
  $("doc-upload-form").classList.toggle("hidden");
  const people = await api("/org/employees");
  fillSelect($("doc-allowed"), people.map(p => ({ value: p.id, label: `${p.name} (${p.role}, ${p.department})` })));
  if (RANK[ME.role] < RANK.admin) $("doc-dept").value = ME.department;
});
$("doc-cancel-btn").addEventListener("click", () => $("doc-upload-form").classList.add("hidden"));
document.querySelectorAll("[data-doctab]").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll("[data-doctab]").forEach(x => x.classList.toggle("active", x === t));
  $("doc-file-wrap").classList.toggle("hidden", t.dataset.doctab !== "file");
  $("doc-text-wrap").classList.toggle("hidden", t.dataset.doctab !== "text");
}));
$("doc-visibility").addEventListener("change", e => $("doc-allowed-wrap").classList.toggle("hidden", e.target.value !== "restricted"));
$("doc-filter").addEventListener("change", loadDocuments);

$("doc-submit-btn").addEventListener("click", async () => {
  msg("doc-error", "");
  const asFile = !$("doc-file-wrap").classList.contains("hidden");
  const allowed = [...$("doc-allowed").selectedOptions].map(o => o.value);
  try {
    if (asFile) {
      const f = $("doc-file").files[0];
      if (!f) throw new Error("Choose a file first");
      const fd = new FormData();
      fd.append("file", f); fd.append("title", $("doc-title").value);
      fd.append("visibility", $("doc-visibility").value); fd.append("department", $("doc-dept").value);
      fd.append("allowed_user_ids", allowed.join(","));
      await api("/documents/upload", { method: "POST", body: fd });
    } else {
      await api("/documents", { method: "POST", body: JSON.stringify({
        title: $("doc-title").value, content: $("doc-content").value, visibility: $("doc-visibility").value,
        department: $("doc-dept").value, allowed_user_ids: allowed.map(Number) }) });
    }
    $("doc-upload-form").classList.add("hidden");
    ["doc-title", "doc-content", "doc-file"].forEach(i => $(i).value = "");
    loadDocuments();
  } catch (err) { msg("doc-error", err.message); }
});

// -------------------------------------------------------------- folders ---
let FOLDERS = [];
let ACTIVE_FOLDER = null;   // folder id or null = all documents
const SCOPE_LABEL = { personal: "Private", department: "Department", company: "Company" };

async function loadFolders() {
  try { FOLDERS = await api("/folders"); } catch { FOLDERS = []; }
  const chip = (id, label, n, active) => `<button class="folder-chip ${active ? "active" : ""}" onclick="selectFolder(${id})">${label}${n === null ? "" : ` <span class="muted">${n}</span>`}</button>`;
  $("folder-bar").innerHTML = chip("null", "📄 All documents", null, ACTIVE_FOLDER === null) +
    FOLDERS.map(f => chip(f.id, `${f.scope === "personal" ? "🔒" : "📁"} ${esc(f.name)}`, f.count, ACTIVE_FOLDER === f.id)).join("") +
    `<button class="folder-chip folder-new" onclick="toggleFolderForm()">+ New folder</button>`;
  const f = FOLDERS.find(x => x.id === ACTIVE_FOLDER);
  $("folder-head").innerHTML = f ? `<div class="folder-head"><strong>${f.scope === "personal" ? "🔒" : "📁"} ${esc(f.name)}</strong>
      <span class="badge badge-dept">${SCOPE_LABEL[f.scope]}${f.department ? " · " + esc(f.department) : ""}</span>
      <span class="muted">by ${esc(f.owner || "")}</span>
      ${f.can_edit ? `<button class="btn-ghost" style="margin-left:auto" onclick="deleteFolder(${f.id})">Delete folder</button>` : ""}</div>
      <div class="view-sub" style="margin:0 0 10px">${f.scope === "personal" ? "Only you can see this folder. It's just for organising — it never changes who can open a document." : "Official folder — everyone still only sees the documents they're allowed to open."}</div>` : "";
}
function selectFolder(id) { ACTIVE_FOLDER = id; loadDocuments(); }
function toggleFolderForm() {
  const scopes = [{ value: "personal", label: "Private (only me)" }];
  if (atLeast("manager")) scopes.push({ value: "department", label: "Department (official)" }, { value: "company", label: "Company-wide (official)" });
  fillSelect($("folder-scope"), scopes);
  $("folder-form").classList.toggle("hidden");
  $("folder-name").focus();
}
$("folder-cancel-btn").addEventListener("click", () => $("folder-form").classList.add("hidden"));
$("folder-create-btn").addEventListener("click", async () => {
  msg("folder-error", "");
  try {
    const f = await api("/folders", { method: "POST", body: JSON.stringify({
      name: $("folder-name").value, scope: $("folder-scope").value, department: ME.department }) });
    $("folder-name").value = ""; $("folder-form").classList.add("hidden");
    ACTIVE_FOLDER = f.id; loadDocuments();
  } catch (err) { msg("folder-error", err.message); }
});
async function deleteFolder(id) {
  if (!confirm("Delete this folder? The documents inside are not deleted.")) return;
  try { await api(`/folders/${id}`, { method: "DELETE" }); ACTIVE_FOLDER = null; loadDocuments(); }
  catch (err) { alert(err.message); }
}
async function addToFolder(sel, docId) {
  const fid = sel.value; if (!fid) return;
  try { await api(`/folders/${fid}/items`, { method: "POST", body: JSON.stringify({ document_id: docId }) }); }
  catch (err) { alert(err.message); }
  if (CUR_DOC && CUR_DOC.id === docId && $("view-doc").classList.contains("active")) openDoc(docId, CUR_HL); else loadDocuments();
}
async function removeFromFolder(fid, docId) {
  try { await api(`/folders/${fid}/items/${docId}`, { method: "DELETE" }); }
  catch (err) { alert(err.message); }
  if (CUR_DOC && CUR_DOC.id === docId && $("view-doc").classList.contains("active")) openDoc(docId, CUR_HL); else loadDocuments();
}
function folderPicker(docId, exclude) {
  const opts = FOLDERS.filter(f => f.can_edit && !(exclude || []).includes(f.id));
  return opts.length ? `<select class="folder-pick" onchange="addToFolder(this,${docId})"><option value="">📁 Add to folder…</option>${
    opts.map(f => `<option value="${f.id}">${esc(f.name)} (${SCOPE_LABEL[f.scope].toLowerCase()})</option>`).join("")}</select>` : "";
}

// ------------------------------------------------------------ documents ---
async function loadDocuments() {
  await loadFolders();
  const filter = $("doc-filter").value;
  let docs = await api("/documents" + (ACTIVE_FOLDER !== null ? `?folder_id=${ACTIVE_FOLDER}` : ""));
  if (filter) docs = docs.filter(d => d.department === filter || d.department === "All");
  $("doc-list").innerHTML = docs.length ? docs.map(d => {
    const review = window.reviewBadge ? window.reviewBadge(d) : (d.needs_review
      ? `<span class="badge badge-stale">⏳ Needs review</span>`
      : `<span class="badge badge-ok">✅ Verified until ${new Date(d.verified_until).toLocaleDateString()}</span>`);
    return `<div class="doc-row"><div class="doc-row-top"><a class="doc-title doc-link" href="#" onclick="openDoc(${d.id});return false">${esc(d.title)}</a>
      <span><span class="badge badge-file">${esc(d.file_type)}</span>
      <span class="badge badge-dept">${esc(d.department)}</span>
      <span class="badge badge-${d.visibility}">${d.visibility}</span> ${review}</span></div>
      <div class="doc-content">${esc(d.preview)}</div>
      <div class="action-row">
        <button class="btn-secondary" onclick="openDoc(${d.id})">📖 Open</button>
        <button class="btn-ghost" onclick="summarizeDoc(${d.id})">📝 Summarize</button>
        <button class="btn-ghost" onclick="askAboutDoc(${d.id})">💬 Ask about this</button>
        <button class="btn-ghost" onclick="verifyDoc(${d.id})">✅ Mark reviewed</button>
        <button class="btn-ghost" onclick="requestDocUpdate(${d.id})">✋ Request update</button>
        ${ACTIVE_FOLDER !== null && (FOLDERS.find(f => f.id === ACTIVE_FOLDER) || {}).can_edit ? `<button class="btn-ghost" onclick="removeFromFolder(${ACTIVE_FOLDER},${d.id})">↩ Remove from folder</button>` : folderPicker(d.id)}
      </div><div class="doc-summary hidden" id="doc-summary-${d.id}"></div></div>`;
  }).join("") : `<div class='empty-note'>${ACTIVE_FOLDER !== null ? "This folder is empty (or holds documents you can't open). Use “Add to folder” on any document." : "No documents yet."}</div>`;
  if (window.decorateDocs) await window.decorateDocs(docs);
}

async function verifyDoc(id) {
  try { await api(`/documents/${id}/verify`, { method: "POST", body: JSON.stringify({ days: 90 }) });
        if ($("view-doc").classList.contains("active")) openDoc(id, CUR_HL); else loadDocuments(); }
  catch (err) { alert(err.message); }
}
async function requestDocUpdate(id) {
  const note = prompt("What needs updating? (optional)") || "";
  try {
    const r = await api(`/documents/${id}/request-update`, { method: "POST", body: JSON.stringify({ note }) });
    alert(r.routed_to ? `Sent to ${r.routed_to}.` : "Sent, but this document has no owner set.");
  } catch (err) { alert(err.message); }
}
async function summarizeDoc(id, boxId) {
  const box = $(boxId || `doc-summary-${id}`);
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = `<div class="empty-note">Summarizing…</div>`;
  try {
    const r = await api(`/documents/${id}/summary`);
    box.innerHTML = `<strong>Summary</strong> <span class="muted">(key sentences pulled from the document)</span><ul>${
      (r.points || [r.summary]).map(p => `<li>${esc(p)}</li>`).join("")}</ul>`;
  } catch (err) { box.innerHTML = `<div class="denied-note">${esc(err.message)}</div>`; }
}
function askAboutDoc(id) {
  api(`/documents/${id}`).then(d => { showView("ask"); newChat({ id: d.id, title: d.title }); }).catch(e => alert(e.message));
}

// ------------------------------------------------------- document viewer ---
let CUR_DOC = null, CUR_HL = null, PREV_VIEW = "documents";
$("doc-back-btn").addEventListener("click", () => showView(PREV_VIEW));

function markedText(content, hl) {
  if (!hl) return esc(content);
  const words = hl.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return esc(content);
  try {
    const re = new RegExp(words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+"), "i");
    const m = re.exec(content);
    if (m) return esc(content.slice(0, m.index)) + `<mark id="doc-hl">${esc(m[0])}</mark>` + esc(content.slice(m.index + m[0].length));
  } catch {}
  return esc(content);
}

async function openDoc(id, hl) {
  const active = document.querySelector(".view.active");
  if (active && active.id !== "view-doc") PREV_VIEW = active.id.replace("view-", "");
  try {
    const d = await api(`/documents/${id}`);
    CUR_DOC = d; CUR_HL = hl || null;
    if (!FOLDERS.length) { try { FOLDERS = await api("/folders"); } catch {} }
    const review = window.reviewBadge ? window.reviewBadge(d) : (d.needs_review
      ? `<span class="badge badge-stale">⏳ Needs review</span>`
      : `<span class="badge badge-ok">✅ Verified until ${new Date(d.verified_until).toLocaleDateString()}</span>`);
    $("doc-viewer").innerHTML = `
      <div class="view-header-row"><h2 style="margin:0">${esc(d.title)}</h2></div>
      <div style="margin:8px 0 12px"><span class="badge badge-file">${esc(d.file_type)}</span>
        <span class="badge badge-dept">${esc(d.department)}</span>
        <span class="badge badge-${d.visibility}">${d.visibility}</span> ${review}</div>
      <div class="muted" style="font-size:13px;margin-bottom:12px">
        Owner: ${esc(d.owner || "—")} · Uploaded by ${esc(d.uploaded_by || "—")} on ${new Date(d.created_at + "Z").toLocaleDateString()}
        ${d.last_reviewed_at ? ` · Last reviewed ${new Date(d.last_reviewed_at + "Z").toLocaleDateString()}${d.last_reviewed_by ? " by " + esc(d.last_reviewed_by) : ""}` : ""}</div>
      <div class="action-row" style="margin:0 0 12px">
        <button class="btn-primary" style="margin:0" onclick="askAboutDoc(${d.id})">💬 Ask about this document</button>
        <button class="btn-secondary" onclick="summarizeDoc(${d.id}, 'viewer-summary')">📝 Summarize</button>
        <button class="btn-ghost" onclick="verifyDoc(${d.id})">✅ Mark reviewed</button>
        <button class="btn-ghost" onclick="requestDocUpdate(${d.id})">✋ Request update</button>
        ${folderPicker(d.id, d.folders.map(f => f.id))}
      </div>
      ${d.folders.length ? `<div class="folder-tags">In: ${d.folders.map(f => `<span class="folder-tag">${f.scope === "personal" ? "🔒" : "📁"} ${esc(f.name)}${f.can_edit ? ` <a href="#" title="Remove from folder" onclick="removeFromFolder(${f.id},${d.id});return false">✕</a>` : ""}</span>`).join(" ")}</div>` : ""}
      <div class="doc-summary hidden" id="viewer-summary"></div>
      ${hl ? `<div class="hl-note">Showing where the answer came from — highlighted below.</div>` : ""}
      <div class="doc-fulltext">${markedText(d.content, hl)}</div>`;
    showView("doc");
    if (window.decorateViewer) window.decorateViewer(d);
    const mk = $("doc-hl"); if (mk) setTimeout(() => mk.scrollIntoView({ block: "center", behavior: "smooth" }), 80);
  } catch (err) { alert(err.message); }
}

// ----------------------------------------------------- cross-dept access ---
async function loadAccessRequests() {
  fillSelect($("acc-dept"), ME.departments.filter(d => d !== ME.department && d !== "All"));
  const r = await api("/access-requests");
  $("acc-mine").innerHTML = r.mine.length ? "<h3>Your requests</h3>" + r.mine.map(a =>
    `<div class="conflict-card"><strong>${esc(a.department)}</strong> — ${a.status}
      ${a.reason ? `<div class="view-sub" style="margin:2px 0 0;font-size:12px">${esc(a.reason)}</div>` : ""}</div>`).join("") : "";
  $("acc-review-wrap").classList.toggle("hidden", !r.for_review.length);
  $("acc-review").innerHTML = r.for_review.map(a =>
    `<div class="conflict-card"><strong>${esc(a.requester)}</strong> wants ${a.duration_hours}h access to <strong>${esc(a.department)}</strong>
      ${a.reason ? `<div class="view-sub" style="margin:2px 0 6px;font-size:12px">${esc(a.reason)}</div>` : ""}
      <div class="action-row"><button class="btn-primary" onclick="decideAccess(${a.id},true)">Approve</button>
      <button class="btn-ghost" onclick="decideAccess(${a.id},false)">Deny</button></div></div>`).join("");
}
$("acc-request-btn").addEventListener("click", async () => {
  try {
    await api("/access-requests", { method: "POST", body: JSON.stringify({
      department: $("acc-dept").value, reason: $("acc-reason").value, duration_hours: Number($("acc-hours").value) || 24 }) });
    $("acc-reason").value = ""; msg("acc-error", "");
    loadAccessRequests();
  } catch (err) { msg("acc-error", err.message); }
});
async function decideAccess(id, approve) {
  try { await api(`/access-requests/${id}/decide`, { method: "POST", body: JSON.stringify({ approve }) }); loadAccessRequests(); }
  catch (err) { alert(err.message); }
}

// ---------------------------------------------------------------- graph ---
$("new-fact-btn").addEventListener("click", () => $("fact-form").classList.toggle("hidden"));
$("fact-cancel-btn").addEventListener("click", () => $("fact-form").classList.add("hidden"));
$("fact-submit-btn").addEventListener("click", async () => {
  try {
    const r = await api("/facts", { method: "POST", body: JSON.stringify({
      subject: $("fact-subject").value, relation: $("fact-relation").value, object: $("fact-object").value }) });
    if (r.conflict_id) msg("fact-error", `Added, but it conflicts with an existing fact — see Conflicts (#${r.conflict_id}).`, true);
    else { $("fact-form").classList.add("hidden"); ["fact-subject", "fact-relation", "fact-object"].forEach(i => $(i).value = ""); msg("fact-error", ""); }
    loadGraph();
  } catch (err) { msg("fact-error", err.message); }
});
async function loadGraph() {
  const d = await api("/graph");
  const box = $("graph-canvas");
  if (!d.nodes.length) { box.innerHTML = "<div class='empty-note'>No facts yet — add one above.</div>"; return; }
  const nodes = new vis.DataSet(d.nodes.map(n => ({ id: n.id, label: n.label, color: { background: "#111827", border: "#22d3ee" }, font: { color: "#f4f7ff" } })));
  const edges = new vis.DataSet(d.edges.map((e, i) => ({ id: i, from: e.from, to: e.to, label: e.label, arrows: "to",
    color: { color: e.status === "conflicting" ? "#f87171" : "#8b5cf6" }, font: { color: "#93a1c2", size: 11, strokeWidth: 0 } })));
  network = new vis.Network(box, { nodes, edges }, { physics: { stabilization: true }, edges: { smooth: { type: "dynamic" } } });
}

// ------------------------------------------------------------ conflicts ---
async function loadConflicts() {
  const rows = await api("/conflicts");
  const canResolve = atLeast("manager");
  $("conflict-list").innerHTML = rows.length ? rows.map(c => {
    const prop = c.proposed_keep_id ? `<div class="view-sub" style="margin:6px 0 0;font-size:12px">📝 Proposed by ${esc(c.proposed_by)}${c.proposed_note ? " — " + esc(c.proposed_note) : ""} — waiting for approval</div>` : "";
    const action = canResolve
      ? `onclick="resolveConflict(${c.conflict_id},${c.old.id})"` : `onclick="proposeConflict(${c.conflict_id},${c.old.id})"`;
    const action2 = canResolve
      ? `onclick="resolveConflict(${c.conflict_id},${c.new.id})"` : `onclick="proposeConflict(${c.conflict_id},${c.new.id})"`;
    return `<div class="conflict-card"><strong>${esc(c.old.subject)} — ${esc(c.old.relation)}</strong>
      <div class="conflict-versions">
        <div class="conflict-version" ${action}><div class="conflict-version-label">Older version</div>${esc(c.old.object)}</div>
        <div class="conflict-version" ${action2}><div class="conflict-version-label">Newer version</div>${esc(c.new.object)}</div>
      </div><div class="view-sub" style="margin:0;font-size:12px">${canResolve ? "Click the version to keep." : "Click the version you believe is right — a manager or admin will approve it."}</div>${prop}</div>`;
  }).join("")
    : "<div class='empty-note'>No open conflicts. 🎉</div>";
}

async function proposeConflict(id, keepId) {
  const note = prompt("Why do you think this version is right? (optional)") || "";
  try { await api(`/conflicts/${id}/propose`, { method: "POST", body: JSON.stringify({ keep_fact_id: keepId, note }) }); loadConflicts(); }
  catch (err) { alert(err.message); }
}
async function resolveConflict(cid, keep) {
  try { await api(`/conflicts/${cid}/resolve`, { method: "POST", body: JSON.stringify({ keep_fact_id: keep }) }); loadConflicts(); }
  catch (err) { alert(err.message); }
}

// -------------------------------------------------------------- reviews ---
async function loadReviews() {
  const rows = await api("/reviews");
  $("review-list").innerHTML = rows.length ? rows.map(r => `
    <div class="review-card" id="rev-${r.id}">
      <div class="muted">${esc(r.asker)} · ${esc(r.department)} · ${fmt(r.created_at)}</div>
      <h4 style="margin:6px 0">${esc(r.question)}</h4>
      <div class="passage"><strong>Answer given:</strong> ${esc(r.answer || "(no answer)")}
        ${r.sources.filter(s => s.document_title).map(s => `<div class="passage-src">source: ${esc(s.document_title)}</div>`).join("")}</div>
      ${r.flag_note ? `<div class="muted">🚩 ${esc(r.asker)} says: ${esc(r.flag_note)}</div>` : ""}
      <textarea rows="2" id="rev-ans-${r.id}" placeholder="Write the correct answer (needed for 'Correct')…"></textarea>
      <div class="row-gap" style="margin-top:8px">
        <input type="text" id="rev-s-${r.id}" placeholder="Optional graph fact: subject">
        <input type="text" id="rev-r-${r.id}" placeholder="relation">
        <input type="text" id="rev-o-${r.id}" placeholder="object">
      </div>
      <input type="text" id="rev-note-${r.id}" placeholder="Note to the asker (optional)" style="margin-top:8px">
      <div class="action-row">
        <button class="btn-primary" onclick="review(${r.id},'correct')">✏️ Submit correction</button>
        <button class="btn-secondary" onclick="review(${r.id},'approve')">✅ Answer was right</button>
        <button class="btn-ghost" onclick="review(${r.id},'reject')">❌ Mark wrong</button>
      </div><div class="auth-error" id="rev-msg-${r.id}"></div></div>`).join("")
    : "<div class='empty-note'>Nothing waiting for review. 🎉</div>";
}
async function review(id, verdict) {
  const s = $(`rev-s-${id}`).value, r = $(`rev-r-${id}`).value, o = $(`rev-o-${id}`).value;
  const body = { verdict, corrected_answer: $(`rev-ans-${id}`).value, note: $(`rev-note-${id}`).value,
    fact: s && r && o ? { subject: s, relation: r, object: o } : null };
  try { await api(`/reviews/${id}`, { method: "POST", body: JSON.stringify(body) }); loadReviews(); refreshPills(); }
  catch (err) { msg(`rev-msg-${id}`, err.message); }
}

// ------------------------------------------------------------- my work ---
async function loadMyWork() {
  const [w, people, inbox] = await Promise.all([api("/my-work"), api("/org/employees"), api("/updates")]);
  const assignable = people.filter(p => p.id !== ME.id && RANK[p.role] < RANK[ME.role] && atLeast("manager") &&
    (ME.role !== "manager" || p.department === ME.department));
  fillSelect($("task-assignee"), assignable.map(p => ({ value: p.id, label: `Assign to ${p.name} (${p.role})` })), "Just me (private to-do)");
  const row = (t, mine) => `<div class="task-row ${t.status === "done" ? "done" : ""}"><div>
      <div>${esc(t.title)} ${t.personal ? '<span class="badge badge-internal">private</span>' : ""}</div>
      <div class="task-meta">${mine ? "" : "Assigned to " + esc(t.owner) + " · "}${t.deadline ? "Due " + esc(t.deadline) : "No deadline"}</div></div>
      ${t.status === "done" ? "✅" : `<button class="btn-ghost" onclick="completeTask(${t.id})">Done</button>`}</div>`;
  $("my-tasks").innerHTML = w.tasks.length ? w.tasks.map(t => row(t, true)).join("") : "<div class='empty-note'>Nothing to do. Add a to-do above.</div>";
  $("delegated-h").classList.toggle("hidden", !w.delegated.length);
  $("delegated-tasks").innerHTML = w.delegated.map(t => row(t, false)).join("");
  const icon = { used_for: "📌", progress: "📈", review_request: "🚩", review_result: "🧑‍⚖️", review_asked: "🔍", review_assigned: "👤", review_done: "✅", review_escalated: "⏫", recall: "📣", rereview: "♻️", knowledge_gap: "🕳️", gap_closed: "💡", access_request: "🔑" };
  $("inbox").innerHTML = inbox.length ? inbox.map(u => `<div class="inbox-row ${u.read ? "" : "unread"}">
      <div class="inbox-meta">${icon[u.kind] || "•"} ${esc(u.from)} · ${fmt(u.created_at)}</div>${esc(u.text)}</div>`).join("")
    : "<div class='empty-note'>Inbox is empty.</div>";
  refreshPills();
}
$("task-submit-btn").addEventListener("click", async () => {
  const assignee = $("task-assignee").value;
  try {
    await api("/tasks", { method: "POST", body: JSON.stringify({ title: $("task-title").value,
      deadline: $("task-deadline").value || null, assignee_id: assignee ? Number(assignee) : null, personal: !assignee }) });
    $("task-title").value = ""; $("task-deadline").value = ""; msg("task-error", ""); loadMyWork();
  } catch (err) { msg("task-error", err.message); }
});
async function completeTask(id) { try { await api(`/tasks/${id}/complete`, { method: "POST" }); loadMyWork(); } catch (e) { alert(e.message); } }
$("mark-read-btn").addEventListener("click", async () => { await api("/updates/read-all", { method: "POST" }); loadMyWork(); });

// --------------------------------------------------------------- people ---
$("new-emp-btn").addEventListener("click", () => {
  fillSelect($("emp-role"), ROLES.filter(r => RANK[r] < RANK[ME.role] && r !== "guest" || r === "guest" && atLeast("manager")));
  fillSelect($("emp-dept"), ME.role === "manager" ? [ME.department] : ME.departments);
  $("emp-form").classList.toggle("hidden");
});
$("emp-cancel-btn").addEventListener("click", () => $("emp-form").classList.add("hidden"));
$("emp-submit-btn").addEventListener("click", async () => {
  try {
    const r = await api("/org/employees", { method: "POST", body: JSON.stringify({ name: $("emp-name").value,
      email: $("emp-email").value || null, role: $("emp-role").value, department: $("emp-dept").value }) });
    showCreds(r.name, r.username, r.temp_password);
    $("emp-form").classList.add("hidden"); $("emp-name").value = ""; $("emp-email").value = ""; msg("emp-error", "");
    loadPeople();
  } catch (err) { msg("emp-error", err.message); }
});
function showCreds(name, username, pwd) {
  const b = $("cred-box");
  b.innerHTML = `<strong>Login for ${esc(name)}</strong> — shown once, copy it now.<br><br>
    Username: <code>${esc(username)}</code> &nbsp; Temporary password: <code>${esc(pwd)}</code>
    <div class="muted" style="margin-top:8px">They must change the password on first login.</div>`;
  b.classList.remove("hidden");
}
async function loadPeople() {
  const people = await api("/org/employees");
  const byId = Object.fromEntries(people.map(p => [p.id, p.name]));
  const detail = atLeast("manager"), admin = atLeast("admin");
  if (admin) {
    const pending = await api("/org/employees/pending");
    $("people-pending").innerHTML = pending.length ? pending.map(p =>
      `<div class="conflict-card"><strong>${esc(p.name)}</strong> — ${esc(p.role)}, ${esc(p.department)} — added by a manager, waiting on your approval
       <div class="action-row"><button class="btn-primary" onclick="approveEmployee(${p.id})">Approve</button></div></div>`).join("") : "";
    $("people-pending-wrap").classList.toggle("hidden", !pending.length);
  }
  $("people-list").innerHTML = people.map(p => `
    <div class="person-row ${p.active === false ? "inactive" : ""}"><div>
      <strong>${esc(p.name)}</strong> <span class="badge badge-dept">${esc(p.department)}</span>
      <span class="badge badge-internal">${p.role}</span>
      <div class="muted">${detail ? "@" + esc(p.username) + (p.email ? " · " + esc(p.email) : "") + " · " : ""}#${p.id}${p.supervisor_id ? " · reports to " + esc(byId[p.supervisor_id] || "?") : ""}</div></div>
      <div class="action-row" style="margin:0">
      ${detail && RANK[p.role] < RANK[ME.role] ? `<button class="btn-ghost" onclick="resetPw(${p.id})">Reset password</button>` : ""}
      ${admin && RANK[p.role] < RANK[ME.role] ? `<button class="btn-ghost" onclick="setActive(${p.id},${p.active === false})">${p.active === false ? "Reactivate" : "Deactivate"}</button>
        <button class="btn-ghost" onclick="changeRole(${p.id})">Role/Dept</button>` : ""}</div></div>`).join("");
}
async function approveEmployee(id) {
  try { await api(`/org/employees/${id}/approve`, { method: "POST" }); loadPeople(); }
  catch (err) { alert(err.message); }
}
async function resetPw(id) {
  if (!confirm("Generate a new temporary password for this person?")) return;
  try { const r = await api(`/org/employees/${id}/reset-password`, { method: "POST" }); showCreds(r.username, r.username, r.temp_password); } catch (e) { alert(e.message); }
}
async function setActive(id, active) { try { await api(`/org/employees/${id}`, { method: "PATCH", body: JSON.stringify({ active }) }); loadPeople(); } catch (e) { alert(e.message); } }
async function changeRole(id) {
  const role = prompt(`New role (${ROLES.filter(r => RANK[r] < RANK[ME.role]).join(", ")}) — blank to keep`);
  const dept = prompt(`New department (${ME.departments.join(", ")}) — blank to keep`);
  const body = {}; if (role) body.role = role; if (dept) body.department = dept;
  if (!Object.keys(body).length) return;
  try { await api(`/org/employees/${id}`, { method: "PATCH", body: JSON.stringify(body) }); loadPeople(); } catch (e) { alert(e.message); }
}

// ------------------------------------------------------------- settings ---
async function loadSettings() {
  const s = await api("/org/settings");
  $("set-name").value = s.name; $("set-accent").value = s.accent; $("set-depts").value = s.departments.join(", ");
}
$("set-save-btn").addEventListener("click", async () => {
  try {
    await api("/org/settings", { method: "PATCH", body: JSON.stringify({ name: $("set-name").value, accent: $("set-accent").value,
      departments: $("set-depts").value.split(",").map(s => s.trim()).filter(Boolean) }) });
    msg("set-msg", "Saved.", true); ME = await api("/me"); applyAccent(ME.accent); $("me-org").textContent = ME.org_name;
    fillSelect($("doc-filter"), ME.departments, "All departments"); fillSelect($("doc-dept"), ["All", ...ME.departments]);
  } catch (err) { msg("set-msg", err.message); }
});

// ---------------------------------------------------------------- audit ---
async function loadAudit() {
  try {
    const rows = await api("/audit-log");
    $("audit-list").innerHTML = rows.map(r => `<div class="audit-row"><span class="audit-time">${fmt(r.created_at)}</span>
      <span class="audit-actor">${esc(r.actor)}</span> <strong>${esc(r.action)}</strong> ${esc(r.detail || "")}</div>`).join("");
  } catch { $("audit-list").innerHTML = "<div class='empty-note'>Manager role or above required.</div>"; }
}

// -------------------------------------------------------------- account ---
async function loadAccount() {
  ME = await api("/me");
  $("force-pw").classList.toggle("hidden", !ME.must_change_password);
  $("account-card").innerHTML = `<div class="kv">
    <div class="k">Name</div><div>${esc(ME.name)}</div><div class="k">Username</div><div><code>${esc(ME.username)}</code></div>
    <div class="k">Email</div><div>${esc(ME.email || "—")}</div><div class="k">Role</div><div>${esc(ME.role)}</div>
    <div class="k">Department</div><div>${esc(ME.department)}</div><div class="k">Reports to</div><div>${esc(ME.supervisor || "—")}</div>
    <div class="k">Organization</div><div>${esc(ME.org_name)} (#${ME.org_id})</div><div class="k">Member since</div><div>${fmt(ME.member_since)}</div></div>`;
  $("acc-name").value = ME.name;
}
$("acc-name-btn").addEventListener("click", async () => {
  await api("/me", { method: "PATCH", body: JSON.stringify({ name: $("acc-name").value }) });
  ME = await api("/me"); $("me-name").textContent = ME.name; loadAccount();
});
$("pw-btn").addEventListener("click", async () => {
  try {
    await api("/auth/change-password", { method: "POST", body: JSON.stringify({ old_password: $("pw-old").value, new_password: $("pw-new").value }) });
    $("pw-old").value = ""; $("pw-new").value = ""; msg("pw-msg", "Password updated.", true); loadAccount();
  } catch (err) { msg("pw-msg", err.message); }
});

// start: a stored token is only trusted after /me confirms it is valid
if (TOKEN) boot(); else logout();
