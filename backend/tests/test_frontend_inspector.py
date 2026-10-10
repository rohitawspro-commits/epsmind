import json
from pathlib import Path

import pytest

from app.services.frontend_inspector import (
    build_generation_context,
    generate_missing_configs,
    inspect_frontend_repo,
    safe_target,
    write_files,
)


def make_repo(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        path.write_text(content)
    return root


def checks_by_id(report):
    return {c["id"]: c for c in report["checks"]}


VITE_REACT_PKG = {
    "name": "shop",
    "scripts": {"dev": "vite"},
    "dependencies": {"react": "^19.0.0", "react-router-dom": "^7.0.0"},
    "devDependencies": {"vite": "^6.0.0"},
}


def test_vite_spa_missing_everything(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": VITE_REACT_PKG,
            "package-lock.json": "{}",
            "index.html": "<div id=root></div>",
            "src/api.ts": "const url = import.meta.env.VITE_API_URL; const k = import.meta.env.SECRET_KEY; if (import.meta.env.DEV) {}",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    checks = checks_by_id(report)

    assert report["detected_framework"] == "vite"
    assert report["framework_label"] == "Vite (React)"
    assert report["output_dir"] == "dist"
    assert report["status"] == "needs_config"
    assert checks["build_script"]["status"] == "fail"
    assert checks["spa_routing"]["status"] == "fail"
    assert checks["env_template"]["status"] == "fail"
    assert checks["dockerfile"]["status"] == "fail"
    assert checks["env_exposure"]["details"] == ["SECRET_KEY"]
    assert report["env_keys"] == ["SECRET_KEY", "VITE_API_URL"]  # DEV is a builtin
    assert set(report["auto_fixable_items"]) == {"build_script", "spa_routing", "env_template", "dockerfile", "dockerignore"}
    assert 0 < report["health_score"] < 60


def test_autofix_round_trip_reaches_ready(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": VITE_REACT_PKG,
            "package-lock.json": "{}",
            "index.html": "",
            "src/main.tsx": "fetch(import.meta.env.VITE_API_URL)",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    ctx = build_generation_context(str(tmp_path), report)
    generated = generate_missing_configs(report["auto_fixable_items"], report["detected_framework"], ctx)
    paths = {f["path"]: f["content"] for f in generated["files"]}

    assert set(paths) == {"package.json", "nginx.conf", ".env.example", "Dockerfile", ".dockerignore"}
    assert json.loads(paths["package.json"])["scripts"]["build"] == "vite build"
    assert json.loads(paths["package.json"])["dependencies"] == VITE_REACT_PKG["dependencies"]
    assert "try_files $uri $uri/ /index.html;" in paths["nginx.conf"]
    assert "location ^~ /assets/" in paths["nginx.conf"]
    assert "COPY package.json package-lock.json ./" in paths["Dockerfile"]
    assert "RUN npm ci" in paths["Dockerfile"]
    assert "ARG VITE_API_URL" in paths["Dockerfile"]
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in paths["Dockerfile"]
    assert "VITE_API_URL=" in paths[".env.example"]

    result = write_files(tmp_path, generated["files"])
    assert len(result["written"]) == 5
    after = inspect_frontend_repo(str(tmp_path))
    assert after["status"] == "ready"
    assert after["health_score"] == 100


def test_existing_files_are_not_overwritten(tmp_path):
    make_repo(tmp_path, {"Dockerfile": "FROM scratch\n"})
    result = write_files(tmp_path, [{"path": "Dockerfile", "content": "FROM nginx\n"}])
    assert result["written"] == []
    assert (tmp_path / "Dockerfile").read_text() == "FROM scratch\n"
    result = write_files(tmp_path, [{"path": "Dockerfile", "content": "FROM nginx\n"}], overwrite=True)
    assert (tmp_path / "Dockerfile").read_text() == "FROM nginx\n"


def test_cra_complete_repo_is_ready(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {
                "scripts": {"build": "react-scripts build"},
                "dependencies": {"react": "18", "react-scripts": "5", "react-router-dom": "6"},
            },
            "yarn.lock": "",
            "src/index.js": "console.log(process.env.REACT_APP_API)",
            ".env.example": "REACT_APP_API=\n",
            "nginx.conf": "server { location / { try_files $uri /index.html; } }",
            "Dockerfile": "FROM nginx\nCOPY nginx.conf /etc/nginx/conf.d/default.conf\n",
            ".dockerignore": "node_modules\n",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    assert report["detected_framework"] == "cra"
    assert report["package_manager"] == "yarn"
    assert report["output_dir"] == "build"
    assert report["status"] == "ready", [c for c in report["checks"] if c["status"] not in ("pass", "skipped")]
    assert report["health_score"] == 100


def test_redirects_only_is_a_warning(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {"scripts": {"build": "vite build"}, "dependencies": {"vue": "3", "vue-router": "4", "vite": "6"}},
            "public/_redirects": "/*  /index.html  200\n",
        },
    )
    spa = checks_by_id(inspect_frontend_repo(str(tmp_path)))["spa_routing"]
    assert spa["status"] == "warn"
    assert spa["auto_fixable"] is True


def test_no_router_skips_spa_check(tmp_path):
    make_repo(tmp_path, {"package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "6"}}})
    assert checks_by_id(inspect_frontend_repo(str(tmp_path)))["spa_routing"]["status"] == "skipped"


def test_vite_custom_out_dir(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "6"}},
            "vite.config.ts": "export default { build: { outDir: 'public_html' } }",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    assert report["output_dir"] == "public_html"
    ctx = build_generation_context(str(tmp_path), report)
    dockerfile = generate_missing_configs(["dockerfile"], "vite", ctx)["files"]
    by_path = {f["path"]: f["content"] for f in dockerfile}
    assert "COPY --from=build /app/public_html /usr/share/nginx/html" in by_path["Dockerfile"]
    # Dockerfile copies nginx.conf, so it is generated alongside.
    assert "nginx.conf" in by_path


def test_next_standalone(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {"scripts": {"build": "next build"}, "dependencies": {"next": "16", "react": "19"}},
            "pnpm-lock.yaml": "",
            "next.config.ts": "const c = { output: 'standalone' }; export default c;",
            "public/favicon.ico": "",
            "app/page.tsx": "export default () => process.env.NEXT_PUBLIC_SITE + process.env.DATABASE_URL",
            ".nvmrc": "v20.11.0",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    assert report["detected_framework"] == "nextjs"
    assert report["runtime"] == "node"
    assert report["next_mode"] == "standalone"
    assert report["container_port"] == 3000
    assert report["node_version"] == "20"
    assert checks_by_id(report)["spa_routing"]["status"] == "skipped"

    ctx = build_generation_context(str(tmp_path), report)
    out = generate_missing_configs(["Dockerfile", ".env.example"], "nextjs", ctx)
    files = {f["path"]: f["content"] for f in out["files"]}
    assert "nginx.conf" not in files
    df = files["Dockerfile"]
    assert "FROM node:20-alpine AS deps" in df
    assert "pnpm install --frozen-lockfile" in df
    assert "COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./" in df
    assert "COPY --from=build --chown=nextjs:nodejs /app/public ./public" in df
    assert 'CMD ["node", "server.js"]' in df
    assert "ARG NEXT_PUBLIC_SITE" in df and "ARG DATABASE_URL" not in df
    env = files[".env.example"]
    assert env.index("NEXT_PUBLIC_SITE=") < env.index("DATABASE_URL=")


def test_next_server_without_public_dir(tmp_path):
    make_repo(tmp_path, {"package.json": {"scripts": {"build": "next build"}, "dependencies": {"next": "16"}}})
    report = inspect_frontend_repo(str(tmp_path))
    out = generate_missing_configs(["dockerfile"], "nextjs", build_generation_context(str(tmp_path), report))
    df = out["files"][0]["content"]
    assert "/app/public" not in df
    assert "next\", \"start\"" in df
    assert out["notes"]  # recommends output: standalone


def test_next_export_uses_nginx(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {"scripts": {"build": "next build"}, "dependencies": {"next": "16"}},
            "next.config.mjs": "export default { output: 'export' }",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    assert report["output_dir"] == "out"
    assert checks_by_id(report)["spa_routing"]["status"] == "fail"
    files = {f["path"]: f["content"] for f in generate_missing_configs(["dockerfile"], "nextjs", build_generation_context(str(tmp_path), report))["files"]}
    assert "try_files $uri $uri.html $uri/ =404;" in files["nginx.conf"]
    assert "/app/out /usr/share/nginx/html" in files["Dockerfile"]


def test_static_site(tmp_path):
    make_repo(tmp_path, {"index.html": "<h1>hi</h1>", "about.html": ""})
    report = inspect_frontend_repo(str(tmp_path))
    assert report["detected_framework"] == "static"
    assert checks_by_id(report)["package_json"]["status"] == "skipped"
    out = generate_missing_configs(report["auto_fixable_items"], "static", build_generation_context(str(tmp_path), report))
    files = {f["path"]: f["content"] for f in out["files"]}
    assert files["Dockerfile"].count("FROM ") == 1
    assert "nginx.conf" in files


def test_missing_package_json_is_blocked(tmp_path):
    make_repo(tmp_path, {"src/app.js": "console.log(1)"})
    report = inspect_frontend_repo(str(tmp_path))
    assert report["status"] == "blocked"
    assert report["detected_framework"] == "unknown"


def test_unsupported_framework_is_named(tmp_path):
    make_repo(tmp_path, {"package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"@sveltejs/kit": "2", "vite": "6"}}})
    report = inspect_frontend_repo(str(tmp_path))
    assert report["framework_label"] == "SvelteKit (not supported yet)"
    assert report["status"] == "blocked"


def test_monorepo_frontend_folder(tmp_path):
    make_repo(tmp_path, {"frontend/package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "6"}}, "backend/main.py": ""})
    report = inspect_frontend_repo(str(tmp_path))
    assert report["app_root"] == "frontend"
    assert report["detected_framework"] == "vite"


def test_env_template_merge_keeps_existing(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "6"}},
            "src/a.ts": "import.meta.env.VITE_A; import.meta.env.VITE_B",
            ".env.example": "# docs\nVITE_A=http://localhost\n",
        },
    )
    report = inspect_frontend_repo(str(tmp_path))
    env = checks_by_id(report)["env_template"]
    assert env["status"] == "warn" and env["details"] == ["VITE_B"]
    out = generate_missing_configs(["env_template"], "vite", build_generation_context(str(tmp_path), report))
    content = out["files"][0]["content"]
    assert content.startswith("# docs\nVITE_A=http://localhost\n")
    assert "VITE_B=" in content
    write_files(tmp_path, out["files"], overwrite=True)
    assert checks_by_id(inspect_frontend_repo(str(tmp_path)))["env_template"]["status"] == "pass"


def test_node_modules_is_not_scanned(tmp_path):
    make_repo(
        tmp_path,
        {
            "package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "6"}},
            "node_modules/lib/index.js": "process.env.SHOULD_NOT_APPEAR",
        },
    )
    assert inspect_frontend_repo(str(tmp_path))["env_keys"] == []


@pytest.mark.parametrize("bad", ["../evil", "/etc/passwd", "a/../../x", "..\\x", ""])
def test_safe_target_rejects_escapes(tmp_path, bad):
    with pytest.raises(ValueError):
        safe_target(tmp_path, bad)


def test_safe_target_rejects_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "public").symlink_to(outside)
    with pytest.raises(ValueError):
        safe_target(repo, "public/_redirects")


def test_write_replaces_symlink_instead_of_following(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").symlink_to(victim)
    write_files(repo, [{"path": "Dockerfile", "content": "FROM nginx\n"}], overwrite=True)
    assert victim.read_text() == "keep"
    assert (repo / "Dockerfile").read_text() == "FROM nginx\n"
