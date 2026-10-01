"""Browser smoke test (Playwright + real Chromium). Starts its own server on a temporary, freshly seeded copy of the
project, so your real anamnesis.db is never touched.

One-time setup (Windows PowerShell, inside the venv):
    pip install pytest playwright
    python -m playwright install chromium
Run from the project root:
    python -m pytest frontend_tests -q -p no:warnings
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    tmp = tempfile.mkdtemp(prefix="anamnesis_ui_")
    ignore = shutil.ignore_patterns("venv", ".venv", "__pycache__", "*.db", ".env", "tests", "audit_anchor.json")
    shutil.copytree(os.path.join(ROOT, "backend"), os.path.join(tmp, "backend"), ignore=ignore)
    shutil.copytree(os.path.join(ROOT, "frontend"), os.path.join(tmp, "frontend"))
    be = os.path.join(tmp, "backend")
    r = subprocess.run([sys.executable, "seed.py"], cwd=be, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    port = _free_port()
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                           cwd=be, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/static/theme-v2.css", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        srv.kill()
        pytest.fail("server did not start")
    yield url
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser, base_url):
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    pg.problems = []
    pg.on("pageerror", lambda e: pg.problems.append("pageerror: " + str(e)))
    pg.on("console", lambda m: pg.problems.append("console: " + m.text) if m.type == "error" else None)
    pg.external = []
    pg.on("request", lambda r: pg.external.append(r.url) if not r.url.startswith(base_url) and not r.url.startswith("data:") else None)
    yield pg
    ctx.close()


def login(pg, base_url, who):
    pg.goto(base_url + "/")
    pg.evaluate("localStorage.clear()")
    pg.goto(base_url + "/")
    pg.evaluate("""() => { const a = document.getElementById('auth-screen'); const l = document.getElementById('landing');
                           if (l) l.classList.add('hidden'); if (a) a.classList.remove('hidden'); }""")
    pg.fill("#login-id", who)
    pg.fill("#login-password", "demo1234")
    pg.click("#login-form button[type=submit]")
    pg.wait_for_selector("#app:not(.hidden)", timeout=10000)


def nav(pg, view):
    pg.click(f'.nav-item[data-view="{view}"]')
    pg.wait_for_selector(f"#view-{view}:not(.hidden)", timeout=8000)


def no_problems(pg):
    bad = [p for p in pg.problems if "favicon" not in p]
    assert not bad, bad


def test_landing_page_and_offline_font(page, base_url):
    page.goto(base_url + "/")
    page.wait_for_selector("#landing:not(.hidden)", timeout=8000)
    page.wait_for_load_state("networkidle")
    assert page.evaluate("document.fonts.check('16px \"Schibsted Grotesk\"')")
    assert page.evaluate("[...document.fonts].some(f => f.family.includes('Schibsted') && f.status === 'loaded')")
    assert not page.external, "the page asked the internet for something: " + str(page.external)
    no_problems(page)


def test_login_bell_and_nav(page, base_url):
    login(page, base_url, "asha")
    assert page.inner_text("#me-name").strip()
    page.click(".pl-bell")
    page.wait_for_selector(".pl-panel:not(.hidden)", timeout=5000)
    page.keyboard.press("Escape")
    for v in ("dashboard", "documents", "ask", "audit", "graph"):
        nav(page, v)
    no_problems(page)


def test_audit_verify_saves_and_matches_head(page, base_url):
    login(page, base_url, "asha")
    nav(page, "audit")
    page.click("#au-verify-btn")
    page.wait_for_selector("#au-verify.ok", timeout=8000)
    first = page.inner_text("#au-verify")
    assert "Chain intact" in first and "Head hash" in first
    page.click("#au-verify-btn")
    page.wait_for_function("document.getElementById('au-verify').textContent.includes('Matches the head saved')", timeout=8000)
    page.fill("#au-noted", "0" * 20)
    page.click("#au-verify-btn")
    page.wait_for_selector("#au-verify.bad", timeout=8000)
    assert "not in the chain" in page.inner_text("#au-verify")
    no_problems(page)


def test_evidence_panel_and_search_vs_anamnesis(page, base_url):
    login(page, base_url, "priya")
    nav(page, "ask")
    page.fill("#ask-input", "how often does the vpn password rotate")
    page.press("#ask-input", "Enter")
    page.wait_for_selector(".ev-panel", timeout=20000)
    page.click(".ev-panel summary")
    page.wait_for_selector(".ev-panel .ev-card", timeout=10000)
    assert page.locator(".ev-panel .ev-card").count() >= 1
    page.click("#ask-evcmp-btn")
    page.wait_for_selector(".ev-overlay .ev-cmp", timeout=10000)
    page.wait_for_function("document.querySelector('.ev-overlay .ev-cmp').innerText.length > 80", timeout=10000)
    assert "Search returns documents" in page.inner_text(".ev-overlay")
    no_problems(page)


def test_graph_screen_draws(page, base_url):
    login(page, base_url, "asha")
    nav(page, "graph")
    page.wait_for_selector("#view-graph canvas, #view-graph .evg", timeout=10000)
    no_problems(page)


def test_mobile_menu_opens(browser, base_url):
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    pg = ctx.new_page()
    pg.problems = []
    pg.on("pageerror", lambda e: pg.problems.append(str(e)))
    login(pg, base_url, "asha")
    assert pg.is_visible(".pl-menu")
    pg.click(".pl-menu")
    assert pg.evaluate("document.body.classList.contains('menu-open')")
    pg.click(".nav-item[data-view='ask']")
    assert not pg.evaluate("document.body.classList.contains('menu-open')")
    assert not pg.problems, pg.problems
    ctx.close()
