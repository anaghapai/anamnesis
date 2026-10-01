/* Anamnesis landing v2. Load AFTER app.js. Does not modify app.js or the backend. */
(function () {
  const $ = id => document.getElementById(id);
  const item = (t, p) => `<div class="lp-item"><h3>${t}</h3><p>${p}</p></div>`;

  const HTML = `
<div id="landing" class="lp hidden">
  <header class="lp-nav">
    <div class="brand" style="margin:0"><span class="brand-dot"></span><h1>Anamnesis</h1></div>
    <nav class="lp-links"><a href="#lp-features">Features</a><a href="#lp-how">How it works</a><a href="#lp-access">Access model</a></nav>
    <div class="lp-nav-r"><button class="lp-btn" style="background:none;color:var(--text)" data-go="login">Sign in</button>
      <button class="lp-btn lp-white" data-go="signup">Get started</button></div>
  </header>

  <section class="lp-hero">
    <div class="lp-arc"></div>
    <span class="lp-pill"><b>What's new?</b> Supervisor-verified answers</span>
    <div class="lp-row">
      <h2 class="lp-h1">From scattered documents to trusted answers, <span class="lp-grad">with memory and proof</span></h2>
      <div class="lp-cta">
        <div class="lp-cta-row"><button class="lp-btn lp-solid" data-go="login">Sign in to your workspace</button>
          <button class="lp-btn lp-outline" data-scroll="lp-how">See how it works</button></div>
        <span class="lp-note">Cited, permission-checked, human-verified</span>
      </div>
    </div>
    <p class="lp-sub">Anamnesis is your organization's private brain. Ask in plain words, get the exact passage and its source, and see who verified it, without anyone seeing documents they shouldn't.</p>
  </section>

  <div class="lp-win">
    <div class="lp-bar">anamnesis / IT department</div>
    <div class="lp-body">
      <div class="lp-side"><span class="on">Ask</span><span>Documents</span><span>Knowledge Graph</span><span>Conflicts</span><span>Reviews</span></div>
      <div class="lp-main">
        <div class="lp-q">How often does the VPN password rotate?</div>
        <div class="lp-a"><b style="color:var(--success)">Verified answer</b>
          <p>The VPN password rotates every 90 days.</p>
          <div class="lp-tags"><span class="ok">Reviewed by your IT manager</span><span>Source: VPN Policy</span><span>Visible to IT only</span></div></div>
        <div class="lp-tags"><span>Illustration of a verified answer</span></div>
      </div>
    </div>
  </div>

  <section class="lp-sec" id="lp-features">
    <h2>Everything a company needs to trust its own knowledge</h2>
    <p>Search, permissions, review and history in one place, built around who is allowed to know what.</p>
    <div class="lp-list">
      ${item("Cited answers", "Every answer points to the exact passage and document it came from. Nothing is invented.")}
      ${item("Permission-aware search", "Departments and visibility tiers are checked before anything is searched, not only before it is shown.")}
      ${item("Supervisor review", "Flag a wrong answer and it goes to a supervisor, who can approve, correct or reject it.")}
      ${item("Conflict detection", "When two facts disagree, both are kept and a manager chooses which one stays current.")}
      ${item("Knowledge graph", "Facts link together so questions that span several documents can still be answered.")}
      ${item("Audit trail", "Logins, questions, uploads and reviews are recorded for managers and above.")}
    </div>
  </section>

  <section class="lp-sec" id="lp-how">
    <h2>How it works</h2>
    <p>Three steps, and the loop improves each time someone corrects it.</p>
    <div class="lp-list lp-steps">
      ${item("Add documents", "Upload PDF, Word, PowerPoint or text. Choose the department and who can see it.")}
      ${item("Ask in your own words", "Get the exact passage, how old the source is, and a warning if it may be outdated.")}
      ${item("Verify and improve", "Reviewed answers are served first the next time a similar question is asked.")}
    </div>
  </section>

  <section class="lp-sec" id="lp-access">
    <h2>Access without open signup</h2>
    <p>Organizations are created by approved founders. Employees get a username and a one-time password from their admin, and set their own on first login.</p>
  </section>

  <div class="lp-stats">
    <div><b>6</b><span>Role levels</span></div><div><b>4</b><span>Visibility tiers</span></div><div><b>0</b><span>Generated answers</span></div>
  </div>

  <div class="lp-end">
    <div><h2>Ready to ask your organization a question?</h2><p>Sign in with the username your admin gave you.</p></div>
    <button class="lp-btn lp-solid" data-go="login">Sign in</button>
  </div>

  <footer class="lp-foot">
    <div class="lp-foot-in">
      <div><div class="brand" style="margin:0 0 10px"><span class="brand-dot"></span><h1>Anamnesis</h1></div>
        <p>A private organizational brain with memory, permission and provenance.</p></div>
      <div><a href="#lp-features">Features</a><a href="#lp-how">How it works</a><a href="#lp-access">Access model</a></div>
      <div><button class="lp-link" data-legal="privacy">Privacy</button><button class="lp-link" data-legal="terms">Terms</button></div>
    </div>
    <div class="lp-copy">Anamnesis, a team project</div>
  </footer>
</div>
<dialog class="lp-dlg" id="lp-dlg"><div id="lp-dlg-body"></div><button class="lp-btn lp-outline" id="lp-dlg-x" style="margin-top:12px">Close</button></dialog>`;

  const LEGAL = {
    privacy: `<h3>Privacy</h3>
      <p>Documents, questions and account details are stored in the database of the server that runs Anamnesis. Passwords are stored hashed.</p>
      <p>Your sign-in token stays in your browser until you log out. Managers and above can read the audit log of logins, questions, uploads and reviews.</p>
      <p>This is a prototype. Do not upload real confidential data.</p>`,
    terms: `<h3>Terms</h3>
      <p>Accounts are created by an organization's admin. Only upload documents you have the right to share with your organization's members.</p>
      <p>Answers are extracted from uploaded documents and can be out of date. Check the source before acting on one.</p>
      <p>Anamnesis is provided as is, without warranty.</p>`
  };

  document.body.insertAdjacentHTML("afterbegin", HTML);
  const landing = $("landing"), auth = $("auth-screen"), dlg = $("lp-dlg");

  function showLanding() {
    landing.classList.remove("hidden"); auth.classList.add("hidden"); $("app").classList.add("hidden");
    window.scrollTo(0, 0);
  }
  function showAuth(tab) {
    landing.classList.add("hidden"); auth.classList.remove("hidden");
    const t = document.querySelector(`.auth-tab[data-tab="${tab}"]`); if (t) t.click();
    window.scrollTo(0, 0);
  }

  landing.addEventListener("click", e => {
    const go = e.target.closest("[data-go]"), sc = e.target.closest("[data-scroll]"), lg = e.target.closest("[data-legal]");
    if (go) showAuth(go.dataset.go);
    if (sc) { const el = $(sc.dataset.scroll); if (el) el.scrollIntoView({ behavior: "smooth" }); }
    if (lg) { $("lp-dlg-body").innerHTML = LEGAL[lg.dataset.legal]; dlg.showModal(); }
  });
  $("lp-dlg-x").addEventListener("click", () => dlg.close());

  const back = document.createElement("a");
  back.href = "#"; back.className = "lp-back"; back.textContent = "Back to home";
  back.addEventListener("click", e => { e.preventDefault(); showLanding(); });
  document.querySelector(".auth-card").prepend(back);

  $("logout-btn").addEventListener("click", showLanding);

  if (localStorage.getItem("anamnesis_token")) auth.classList.add("hidden");
  else showLanding();

  // Interface labels only: drop emojis and spaced em dashes. Document text, answers and user-typed content are never touched.
  const CHROME = "button,option,label,.nav-item,h2,h3,.result-block>h4,.verified-block>h4,.badge,.view-sub,.hint-card,.chat-intro,.denied-note,.hl-note,.empty-note,.chat-scope,.answer-src,.stat-label";
  const EMOJI = /[\p{Extended_Pictographic}\uFE0F\u200D]/gu;
  function clean(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (w.nextNode()) nodes.push(w.currentNode);
    nodes.forEach(n => {
      const p = n.parentElement;
      if (!p || p.closest("textarea,script,style,.doc-tab,#lp-dlg") || !p.closest(CHROME)) return;
      const t = n.nodeValue.replace(EMOJI, "").replace(/ — /g, ", ").replace(/^\s{2,}/, "");
      if (t !== n.nodeValue) n.nodeValue = t;
    });
  }
  let queued = false;
  new MutationObserver(() => {
    if (queued) return; queued = true;
    requestAnimationFrame(() => { queued = false; clean(document.body); });
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
  clean(document.body);
})();
