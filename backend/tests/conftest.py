"""Shared test setup. Copies the backend to a temp folder, seeds a fresh demo DB THERE, and serves it with TestClient.
Your real anamnesis.db is never touched. Tests run in file-name order; test_zz_demo_story.py changes data on purpose,
so it is named to run last."""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def client():
    tmp = tempfile.mkdtemp(prefix="anamnesis_test_")
    dst = os.path.join(tmp, "backend")
    shutil.copytree(BACKEND, dst, ignore=shutil.ignore_patterns("venv", ".venv", "tests", "__pycache__", "*.db", ".env"))
    r = subprocess.run([sys.executable, "seed.py"], cwd=dst, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    sys.path.insert(0, dst)
    from app.main import app
    with TestClient(app) as c:
        yield c
    shutil.rmtree(tmp, ignore_errors=True)


def _login(client, who):
    r = client.post("/auth/login", json={"identifier": who, "password": "demo1234"})
    assert r.status_code == 200, f"{who}: {r.status_code} {r.text}"
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="session")
def H(client):
    return {n: _login(client, n) for n in ("asha", "rahul", "meera", "priya", "kabir")}


@pytest.fixture(scope="session")
def ID(client, H):
    """username -> user id, read from the API so tests never guess."""
    out = {}
    for n, h in H.items():
        r = client.get("/me", headers=h)
        assert r.status_code == 200, f"/me failed for {n}: {r.status_code}"
        out[n] = r.json()["id"]
    return out
