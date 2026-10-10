import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.api.preflight as preflight

    monkeypatch.setattr(preflight, "WORKSPACE_ROOT", tmp_path.resolve())
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-web"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19", "react-router-dom": "7"}, "devDependencies": {"vite": "6"}})
    )
    (root / "package-lock.json").write_text("{}")
    (root / "src" / "main.tsx").write_text("fetch(import.meta.env.VITE_API_URL)")
    return root


def test_inspect_endpoint(client, repo):
    res = client.post("/api/inspect/frontend", json={"repo_path": "acme-web"})
    assert res.status_code == 200
    body = res.json()
    assert body["detected_framework"] == "vite"
    assert body["status"] == "needs_config"


def test_inspect_rejects_path_outside_workspace(client):
    assert client.post("/api/inspect/frontend", json={"repo_path": "../../etc"}).status_code == 400
    assert client.post("/api/inspect/frontend", json={"repo_path": "/etc"}).status_code == 400
    assert client.post("/api/inspect/frontend", json={"repo_path": "missing"}).status_code == 404


def test_dry_run_writes_nothing(client, repo):
    res = client.post("/api/autofix/inject", json={"repo_path": "acme-web", "dry_run": True})
    assert res.status_code == 200
    body = res.json()
    assert {f["path"] for f in body["files"]} >= {"Dockerfile", "nginx.conf"}
    assert all(f["content"] for f in body["files"])
    assert not (repo / "Dockerfile").exists()


def test_auto_inject_fixes_repo(client, repo):
    res = client.post("/api/autofix/inject", json={"repo_path": "acme-web"})
    assert res.status_code == 200
    body = res.json()
    assert {w["path"] for w in body["written"]} == {"package.json", "nginx.conf", ".env.example", "Dockerfile", ".dockerignore"}
    assert body["report"]["status"] == "ready"
    assert (repo / "Dockerfile").is_file()


def test_auto_inject_selected_items(client, repo):
    res = client.post("/api/autofix/inject", json={"repo_path": "acme-web", "items": ["dockerignore"]})
    assert [w["path"] for w in res.json()["written"]] == [".dockerignore"]


def test_auto_inject_unknown_item(client, repo):
    assert client.post("/api/autofix/inject", json={"repo_path": "acme-web", "items": ["rm -rf"]}).status_code == 400


def test_manual_upload(client, repo):
    conf = "server { listen 80; location / { try_files $uri /index.html; } }\n"
    res = client.post(
        "/api/autofix/inject",
        json={"repo_path": "acme-web", "mode": "manual", "files": [{"path": "nginx.conf", "content": conf}]},
    )
    assert res.status_code == 200
    assert (repo / "nginx.conf").read_text() == conf
    spa = {c["id"]: c for c in res.json()["report"]["checks"]}["spa_routing"]
    assert spa["status"] == "pass"


def test_manual_upload_rejects_other_paths(client, repo):
    for path in ("src/main.tsx", "../x", ".env", "package.json"):
        res = client.post(
            "/api/autofix/inject",
            json={"repo_path": "acme-web", "mode": "manual", "files": [{"path": path, "content": "x"}]},
        )
        assert res.status_code == 400, path
