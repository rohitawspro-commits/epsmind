"""
OpsMind frontend pre-flight inspector.

Runs a fast, read-only scan over an ingested frontend repository *before* a
container build, scores how deploy-ready it is, and generates the config files
that are missing (Dockerfile, nginx.conf, .env.example, .dockerignore, and a
package.json "build" script).

Config generation is deterministic: every file comes from a template that is
parameterised with facts detected in the repo (package manager, lockfile,
output directory, Next.js output mode, env keys...). That makes the result
reproducible and always syntactically valid, which matters more for a build
gate than free-form LLM output.

Compatible with Python 3.9+.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

FRAMEWORK_NEXT = "nextjs"
FRAMEWORK_VITE = "vite"
FRAMEWORK_CRA = "cra"
FRAMEWORK_VUE_CLI = "vue-cli"
FRAMEWORK_STATIC = "static"
FRAMEWORK_UNKNOWN = "unknown"

SUPPORTED_FRAMEWORKS = (
    FRAMEWORK_NEXT,
    FRAMEWORK_VITE,
    FRAMEWORK_CRA,
    FRAMEWORK_VUE_CLI,
    FRAMEWORK_STATIC,
)

# Meta-frameworks we recognise but do not generate configs for yet. Checked
# before Vite because several of them (SvelteKit, Nuxt) depend on Vite.
UNSUPPORTED_FRAMEWORKS = {
    "@angular/core": "Angular",
    "nuxt": "Nuxt",
    "astro": "Astro",
    "@sveltejs/kit": "SvelteKit",
    "@remix-run/react": "Remix",
    "gatsby": "Gatsby",
}

VITE_FLAVOURS = (
    ("react", "React"),
    ("vue", "Vue"),
    ("svelte", "Svelte"),
    ("preact", "Preact"),
    ("solid-js", "Solid"),
)

CLIENT_ROUTERS = (
    "react-router",
    "react-router-dom",
    "@tanstack/react-router",
    "vue-router",
    "wouter",
    "svelte-routing",
    "svelte-spa-router",
    "@solidjs/router",
    "preact-router",
)

DEFAULT_BUILD_SCRIPTS = {
    FRAMEWORK_NEXT: "next build",
    FRAMEWORK_VITE: "vite build",
    FRAMEWORK_CRA: "react-scripts build",
    FRAMEWORK_VUE_CLI: "vue-cli-service build",
}

DEFAULT_OUTPUT_DIRS = {
    FRAMEWORK_VITE: "dist",
    FRAMEWORK_CRA: "build",
    FRAMEWORK_VUE_CLI: "dist",
}

# Path prefix of content-hashed assets per framework (safe to cache forever).
HASHED_ASSET_PREFIXES = {
    FRAMEWORK_VITE: ["/assets/"],
    FRAMEWORK_CRA: ["/static/"],
    FRAMEWORK_VUE_CLI: ["/js/", "/css/"],
    FRAMEWORK_NEXT: ["/_next/static/"],
}

# Env var name prefixes that a bundler inlines into client code.
PUBLIC_ENV_PREFIXES = {
    FRAMEWORK_VITE: ("VITE_",),
    FRAMEWORK_CRA: ("REACT_APP_",),
    FRAMEWORK_VUE_CLI: ("VUE_APP_",),
    FRAMEWORK_NEXT: ("NEXT_PUBLIC_",),
}

BUILTIN_ENV_KEYS = {
    "NODE_ENV",
    "MODE",
    "DEV",
    "PROD",
    "SSR",
    "BASE_URL",
    "PUBLIC_URL",
    "NEXT_RUNTIME",
    "VERCEL",
    "VERCEL_ENV",
    "VERCEL_URL",
    "CI",
}

LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)

ENV_TEMPLATE_NAMES = (".env.example", ".env.sample", ".env.template", ".env.dist")
COMMITTED_ENV_NAMES = (".env", ".env.local", ".env.production", ".env.production.local", ".env.development.local")

IGNORED_DIRS = {
    "node_modules",
    ".git",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".cache",
    ".vercel",
    ".output",
    "dist",
    "build",
    "out",
    "coverage",
    "vendor",
    "__pycache__",
}
SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"}
MAX_SCANNED_FILES = 5000
MAX_FILE_BYTES = 512 * 1024

# Candidate sub-folders that commonly hold the frontend in a mono-repo.
APP_ROOT_CANDIDATES = ("frontend", "client", "web", "app", "ui", "apps/web", "apps/frontend", "packages/web")

DEFAULT_NODE_MAJOR = "22"
NGINX_IMAGE = "nginx:1.27-alpine"

# Check weights. Skipped checks are excluded from the score.
WEIGHTS = {
    "package_json": 20,
    "build_script": 15,
    "output_dir": 10,
    "spa_routing": 15,
    "env_template": 10,
    "env_exposure": 5,
    "dockerfile": 15,
    "dockerignore": 5,
    "lockfile": 5,
    "committed_env": 5,
}

# Files the auto-fixer may create, keyed by the check that produces them.
FIX_TARGETS = {
    "build_script": "package.json",
    "spa_routing": "nginx.conf",
    "env_template": ".env.example",
    "dockerfile": "Dockerfile",
    "dockerignore": ".dockerignore",
}

# Friendly aliases accepted by generate_missing_configs().
ITEM_ALIASES = {
    "package.json": "build_script",
    "build": "build_script",
    "nginx.conf": "spa_routing",
    "nginx": "spa_routing",
    ".env.example": "env_template",
    "env": "env_template",
    "dockerfile": "dockerfile",
    "Dockerfile": "dockerfile",
    ".dockerignore": "dockerignore",
}

# Files a user may supply by hand through the manual upload path.
MANUAL_ALLOWED_FILES = ("Dockerfile", ".dockerignore", "nginx.conf", ".env.example", "public/_redirects")

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"

_IMPORT_META_ENV_RE = re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)")
_PROCESS_ENV_RE = re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")
_PROCESS_ENV_INDEX_RE = re.compile(r"process\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_REDIRECTS_SPA_RE = re.compile(r"^\s*/\*\s+/index\.html\s+200\b", re.MULTILINE)


class InspectorError(Exception):
    """Raised when a repository cannot be inspected at all."""


# --------------------------------------------------------------------------- #
# Small file helpers
# --------------------------------------------------------------------------- #


def _read_text(path: Path, limit: int = MAX_FILE_BYTES) -> Optional[str]:
    """Read a small regular file as text. Symlinks and large files are ignored."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _first_existing(base: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        candidate = base / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _config_file(app_dir: Path, stem: str) -> Optional[Path]:
    exts = ("ts", "mts", "js", "mjs", "cjs")
    return _first_existing(app_dir, ["{}.{}".format(stem, ext) for ext in exts])


def _iter_source_files(app_dir: Path) -> List[Path]:
    """Collect source files, skipping dependency/build folders and symlinks."""
    files = []  # type: List[Path]
    for root, dirs, filenames in os.walk(str(app_dir), followlinks=False):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix in SOURCE_EXTENSIONS:
                files.append(Path(root) / name)
                if len(files) >= MAX_SCANNED_FILES:
                    return files
    return files


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def _find_app_root(repo: Path) -> Path:
    """Locate the frontend app inside the repo (repo root or a common sub-folder)."""
    if (repo / "package.json").is_file() or (repo / "index.html").is_file():
        return repo
    for candidate in APP_ROOT_CANDIDATES:
        sub = repo / candidate
        if (sub / "package.json").is_file() and not sub.is_symlink():
            return sub
    return repo


def _load_package_json(app_dir: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (parsed package.json, error message)."""
    path = app_dir / "package.json"
    if not path.exists():
        return None, None
    text = _read_text(path)
    if text is None:
        return None, "package.json could not be read (too large, symlinked or unreadable)."
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, "package.json is not valid JSON: {}".format(exc)
    if not isinstance(data, dict):
        return None, "package.json must contain a JSON object."
    return data, None


def _dependencies(pkg: Optional[Dict[str, Any]]) -> Set[str]:
    if not pkg:
        return set()
    deps = set()  # type: Set[str]
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            deps.update(section.keys())
    return deps


def _detect_framework(app_dir: Path, pkg: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (framework id, human label)."""
    deps = _dependencies(pkg)

    if "next" in deps or _config_file(app_dir, "next.config"):
        return FRAMEWORK_NEXT, "Next.js"

    for dep, label in UNSUPPORTED_FRAMEWORKS.items():
        if dep in deps:
            return FRAMEWORK_UNKNOWN, "{} (not supported yet)".format(label)

    if "vite" in deps or _config_file(app_dir, "vite.config"):
        for dep, label in VITE_FLAVOURS:
            if dep in deps:
                return FRAMEWORK_VITE, "Vite ({})".format(label)
        return FRAMEWORK_VITE, "Vite"

    if "react-scripts" in deps:
        return FRAMEWORK_CRA, "Create React App"

    if "@vue/cli-service" in deps:
        return FRAMEWORK_VUE_CLI, "Vue CLI"

    if pkg is None and (app_dir / "index.html").is_file():
        return FRAMEWORK_STATIC, "Static HTML"

    if pkg is not None and (app_dir / "index.html").is_file() and not _build_script(pkg):
        return FRAMEWORK_STATIC, "Static HTML"

    return FRAMEWORK_UNKNOWN, "Unknown"


def _build_script(pkg: Optional[Dict[str, Any]]) -> Optional[str]:
    if not pkg:
        return None
    scripts = pkg.get("scripts")
    if isinstance(scripts, dict):
        value = scripts.get("build")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _detect_package_manager(app_dir: Path, repo: Path, pkg: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """Return (package manager, lockfile name relative to app dir or None)."""
    for name, manager in LOCKFILES:
        if (app_dir / name).is_file():
            return manager, name
    # Workspaces keep the lockfile at the repo root; report the manager anyway.
    if app_dir != repo:
        for name, manager in LOCKFILES:
            if (repo / name).is_file():
                return manager, None
    declared = (pkg or {}).get("packageManager")
    if isinstance(declared, str):
        for manager in ("pnpm", "yarn", "bun", "npm"):
            if declared.startswith(manager + "@"):
                return manager, None
    return "npm", None


def _detect_node_major(app_dir: Path, pkg: Optional[Dict[str, Any]]) -> str:
    for name in (".nvmrc", ".node-version"):
        text = _read_text(app_dir / name, limit=1024)
        if text:
            match = re.search(r"(\d{2})", text)
            if match:
                return match.group(1)
    engines = (pkg or {}).get("engines")
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        spec = engines["node"]
        majors = [int(m) for m in re.findall(r"(\d{2})", spec)]
        if majors:
            # Open-ended ranges (">=18") get our default LTS; bounded ones keep their pin.
            if ">" in spec and "<" not in spec:
                return str(max(majors[0], int(DEFAULT_NODE_MAJOR)))
            return str(majors[0])
    return DEFAULT_NODE_MAJOR


def _regex_value(text: Optional[str], key: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(key + r"\s*:\s*['\"`]([^'\"`]+)['\"`]", text)
    return match.group(1) if match else None


def _detect_output(app_dir: Path, framework: str) -> Dict[str, Any]:
    """Work out where the build lands and how it is served."""
    if framework == FRAMEWORK_STATIC:
        return {"output_dir": ".", "runtime": "static", "next_mode": None, "source": "repository root is served as-is"}

    if framework == FRAMEWORK_NEXT:
        config = _config_file(app_dir, "next.config")
        text = _read_text(config) if config else None
        mode = _regex_value(text, "output") or "server"
        dist_dir = _regex_value(text, "distDir")
        if mode == "export":
            return {
                "output_dir": dist_dir or "out",
                "runtime": "static",
                "next_mode": "export",
                "source": "output: 'export' in {}".format(config.name) if config else "default",
            }
        return {
            "output_dir": dist_dir or ".next",
            "runtime": "node",
            "next_mode": "standalone" if mode == "standalone" else "server",
            "source": config.name if config else "default",
        }

    if framework == FRAMEWORK_VITE:
        config = _config_file(app_dir, "vite.config")
        configured = _regex_value(_read_text(config) if config else None, "outDir")
        return {
            "output_dir": configured or DEFAULT_OUTPUT_DIRS[framework],
            "runtime": "static",
            "next_mode": None,
            "source": "build.outDir in {}".format(config.name) if configured and config else "Vite default",
        }

    if framework == FRAMEWORK_VUE_CLI:
        config = _config_file(app_dir, "vue.config")
        configured = _regex_value(_read_text(config) if config else None, "outputDir")
        return {
            "output_dir": configured or DEFAULT_OUTPUT_DIRS[framework],
            "runtime": "static",
            "next_mode": None,
            "source": "outputDir in {}".format(config.name) if configured and config else "Vue CLI default",
        }

    if framework == FRAMEWORK_CRA:
        return {"output_dir": "build", "runtime": "static", "next_mode": None, "source": "Create React App default"}

    return {"output_dir": None, "runtime": None, "next_mode": None, "source": None}


def _scan_env_keys(app_dir: Path) -> Dict[str, Set[str]]:
    """Find env keys accessed in source code, grouped by access style."""
    meta_keys = set()  # type: Set[str]
    process_keys = set()  # type: Set[str]
    for path in _iter_source_files(app_dir):
        text = _read_text(path)
        if not text or "env" not in text:
            continue
        meta_keys.update(_IMPORT_META_ENV_RE.findall(text))
        process_keys.update(_PROCESS_ENV_RE.findall(text))
        process_keys.update(_PROCESS_ENV_INDEX_RE.findall(text))
    return {
        "import_meta": {k for k in meta_keys if k not in BUILTIN_ENV_KEYS},
        "process": {k for k in process_keys if k not in BUILTIN_ENV_KEYS},
    }


def _parse_env_keys(text: str) -> Set[str]:
    keys = set()  # type: Set[str]
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _find_nginx_configs(app_dir: Path) -> List[Path]:
    found = []  # type: List[Path]
    for pattern in ("*.conf", "nginx/*.conf", "nginx/**/*.conf", "deploy/*.conf", "docker/*.conf", "conf/*.conf", ".docker/*.conf"):
        for path in app_dir.glob(pattern):
            if not path.is_file() or path.is_symlink() or path in found:
                continue
            # Ignore unrelated *.conf files (supervisord, etc.).
            if "nginx" in path.as_posix().lower() or "location" in (_read_text(path) or ""):
                found.append(path)
    return found


def _has_client_router(deps: Set[str]) -> bool:
    return any(router in deps for router in CLIENT_ROUTERS)


def _find_dockerfile(app_dir: Path, repo: Path) -> Optional[Path]:
    for base in (app_dir, repo):
        for name in ("Dockerfile", "dockerfile"):
            path = base / name
            if path.is_file() and not path.is_symlink():
                return path
    return None


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _check(
    check_id: str,
    title: str,
    status: str,
    message: str,
    why: str,
    auto_fixable: bool = False,
    file: Optional[str] = None,
    details: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "weight": WEIGHTS[check_id],
        "message": message,
        "why": why,
        "file": file,
        "auto_fixable": bool(auto_fixable and status in (STATUS_FAIL, STATUS_WARN)),
        "manual_allowed": file in MANUAL_ALLOWED_FILES if file else False,
        "details": details or [],
    }


def _score(checks: List[Dict[str, Any]]) -> int:
    total = 0.0
    earned = 0.0
    for check in checks:
        if check["status"] == STATUS_SKIPPED:
            continue
        total += check["weight"]
        if check["status"] == STATUS_PASS:
            earned += check["weight"]
        elif check["status"] == STATUS_WARN:
            earned += check["weight"] / 2.0
    if total == 0:
        return 0
    return int(round(100 * earned / total))


def inspect_frontend_repo(repo_path: str) -> Dict[str, Any]:
    """
    Run the pre-flight scan on a checked-out frontend repository.

    Returns a JSON-serialisable dict with the overall status, a 0-100 health
    score, the detected framework, every individual check, the files that are
    missing and which of them OpsMind can generate automatically.
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise InspectorError("Repository path does not exist or is not a directory: {}".format(repo_path))

    app_dir = _find_app_root(repo)
    pkg, pkg_error = _load_package_json(app_dir)
    deps = _dependencies(pkg)
    framework, framework_label = _detect_framework(app_dir, pkg)
    package_manager, lockfile = _detect_package_manager(app_dir, repo, pkg)
    output = _detect_output(app_dir, framework)
    is_static_site = framework == FRAMEWORK_STATIC
    dockerfile = _find_dockerfile(app_dir, repo)
    dockerfile_text = _read_text(dockerfile) if dockerfile else None

    checks = []  # type: List[Dict[str, Any]]

    # 1. package.json ------------------------------------------------------- #
    pkg_why = "The build container installs dependencies and runs scripts from package.json."
    if is_static_site:
        checks.append(_check("package_json", "package.json", STATUS_SKIPPED, "Static site: no package.json needed.", pkg_why))
    elif pkg_error:
        checks.append(_check("package_json", "package.json", STATUS_FAIL, pkg_error, pkg_why, file="package.json"))
    elif pkg is None:
        checks.append(
            _check(
                "package_json",
                "package.json",
                STATUS_FAIL,
                "No package.json found at the app root.",
                pkg_why + " It cannot be generated because the dependency list is unknown.",
                file="package.json",
            )
        )
    else:
        checks.append(_check("package_json", "package.json", STATUS_PASS, "package.json found and valid.", pkg_why))

    # 2. build script ------------------------------------------------------- #
    build_why = "OpsMind runs the \"build\" script to produce the deployable bundle."
    build = _build_script(pkg)
    if is_static_site or pkg is None:
        checks.append(_check("build_script", "Build script", STATUS_SKIPPED, "Not applicable.", build_why))
    elif build:
        checks.append(_check("build_script", "Build script", STATUS_PASS, "scripts.build = \"{}\"".format(build), build_why))
    else:
        default = DEFAULT_BUILD_SCRIPTS.get(framework)
        checks.append(
            _check(
                "build_script",
                "Build script",
                STATUS_FAIL,
                "package.json has no \"build\" script."
                + (" OpsMind can add \"{}\".".format(default) if default else ""),
                build_why,
                auto_fixable=default is not None,
                file="package.json",
            )
        )

    # 3. output directory --------------------------------------------------- #
    out_why = "The runtime image copies the build output; an unknown folder means an empty or broken site."
    committed_outputs = [d for d in ("dist", "build", "out") if (app_dir / d).is_dir()]
    if output["output_dir"] is None:
        checks.append(
            _check(
                "output_dir",
                "Build output directory",
                STATUS_FAIL,
                "Could not determine the build output folder for framework \"{}\".".format(framework_label),
                out_why,
            )
        )
    elif len(committed_outputs) > 1 or (committed_outputs and output["output_dir"] not in committed_outputs and not is_static_site):
        checks.append(
            _check(
                "output_dir",
                "Build output directory",
                STATUS_WARN,
                "Build output resolves to \"{}\" but the repo also contains committed {}.".format(
                    output["output_dir"], ", ".join(d + "/" for d in committed_outputs)
                ),
                out_why + " Committed build artifacts are stale and make the target ambiguous.",
                details=["Delete committed build folders and add them to .gitignore."],
            )
        )
    else:
        label = "Node.js server ({})".format(output["output_dir"]) if output["runtime"] == "node" else output["output_dir"] + "/"
        checks.append(
            _check("output_dir", "Build output directory", STATUS_PASS, "{} ({})".format(label, output["source"]), out_why)
        )

    # 4. SPA routing -------------------------------------------------------- #
    spa_why = (
        "Client-side routes such as /dashboard do not exist as files. Without a fallback to index.html, "
        "refreshing or deep-linking returns 404."
    )
    nginx_configs = _find_nginx_configs(app_dir)
    nginx_texts = [(p, _read_text(p) or "") for p in nginx_configs]
    redirects = _first_existing(app_dir, ["public/_redirects", "_redirects", "static/_redirects"])
    redirects_ok = bool(redirects and _REDIRECTS_SPA_RE.search(_read_text(redirects) or ""))
    nginx_spa = [p for p, t in nginx_texts if "try_files" in t and ("index.html" in t or "$uri.html" in t)]
    serve_single = bool(dockerfile_text and re.search(r"serve\b[^\n]*\s(-s|--single)\b", dockerfile_text))

    if output["runtime"] == "node":
        spa_needed, spa_reason = False, "Next.js server handles routing itself."
    elif framework == FRAMEWORK_NEXT and output["next_mode"] == "export":
        spa_needed, spa_reason = True, ""
    elif framework in (FRAMEWORK_VITE, FRAMEWORK_CRA, FRAMEWORK_VUE_CLI):
        spa_needed = _has_client_router(deps)
        spa_reason = "No client-side router dependency detected; default static serving is enough."
    else:
        spa_needed, spa_reason = False, "Not applicable for this project type."

    if not spa_needed:
        checks.append(_check("spa_routing", "SPA routing fallback", STATUS_SKIPPED, spa_reason, spa_why))
    elif nginx_spa or serve_single:
        where = _rel(nginx_spa[0], repo) if nginx_spa else "Dockerfile (serve -s)"
        checks.append(_check("spa_routing", "SPA routing fallback", STATUS_PASS, "Fallback rule found in {}.".format(where), spa_why))
    elif redirects_ok:
        checks.append(
            _check(
                "spa_routing",
                "SPA routing fallback",
                STATUS_WARN,
                "_redirects found, but it is only honoured by Netlify/Cloudflare Pages, not by the nginx container OpsMind deploys.",
                spa_why,
                auto_fixable=True,
                file="nginx.conf",
            )
        )
    else:
        details = []
        if dockerfile_text is not None and "nginx.conf" not in dockerfile_text:
            details.append("Your existing Dockerfile does not copy nginx.conf; add: COPY nginx.conf /etc/nginx/conf.d/default.conf")
        checks.append(
            _check(
                "spa_routing",
                "SPA routing fallback",
                STATUS_FAIL,
                "No nginx.conf with a try_files fallback to /index.html.",
                spa_why,
                auto_fixable=True,
                file="nginx.conf",
                details=details,
            )
        )

    # 5. env template + 6. env exposure ------------------------------------- #
    env_why = "Code reads environment variables; without a template, operators cannot know which values to set and the build ships `undefined`."
    env = _scan_env_keys(app_dir) if not is_static_site else {"import_meta": set(), "process": set()}
    used_keys = sorted(env["import_meta"] | env["process"])
    template_path = _first_existing(app_dir, list(ENV_TEMPLATE_NAMES))
    template_text = _read_text(template_path) if template_path else None

    if not used_keys:
        checks.append(_check("env_template", "Environment template", STATUS_SKIPPED, "No environment variable usage detected.", env_why))
    elif template_path is None:
        checks.append(
            _check(
                "env_template",
                "Environment template",
                STATUS_FAIL,
                "{} env key(s) used in code but no .env.example exists.".format(len(used_keys)),
                env_why,
                auto_fixable=True,
                file=".env.example",
                details=used_keys,
            )
        )
    else:
        undocumented = sorted(set(used_keys) - _parse_env_keys(template_text or ""))
        if undocumented:
            checks.append(
                _check(
                    "env_template",
                    "Environment template",
                    STATUS_WARN,
                    "{} key(s) used in code are missing from {}.".format(len(undocumented), template_path.name),
                    env_why,
                    auto_fixable=template_path.name == ".env.example",
                    file=".env.example" if template_path.name == ".env.example" else None,
                    details=undocumented,
                )
            )
        else:
            checks.append(
                _check("env_template", "Environment template", STATUS_PASS, "All used keys are documented in {}.".format(template_path.name), env_why)
            )

    exposure_why = "Bundlers only inline variables with a public prefix into browser code; others are silently `undefined` at runtime."
    prefixes = PUBLIC_ENV_PREFIXES.get(framework)
    if prefixes and framework in (FRAMEWORK_VITE, FRAMEWORK_CRA, FRAMEWORK_VUE_CLI):
        client_keys = env["import_meta"] if framework == FRAMEWORK_VITE else env["process"]
        unexposed = sorted(k for k in client_keys if not k.startswith(prefixes))
        if unexposed:
            checks.append(
                _check(
                    "env_exposure",
                    "Client env prefixes",
                    STATUS_WARN,
                    "{} key(s) lack the {} prefix and will be undefined in the browser.".format(len(unexposed), "/".join(prefixes)),
                    exposure_why,
                    details=unexposed,
                )
            )
        elif client_keys:
            checks.append(_check("env_exposure", "Client env prefixes", STATUS_PASS, "All client env keys use {}.".format("/".join(prefixes)), exposure_why))
        else:
            checks.append(_check("env_exposure", "Client env prefixes", STATUS_SKIPPED, "No client env keys detected.", exposure_why))
    else:
        checks.append(_check("env_exposure", "Client env prefixes", STATUS_SKIPPED, "Not applicable.", exposure_why))

    # 7. Dockerfile --------------------------------------------------------- #
    docker_why = "OpsMind builds a container image from the Dockerfile; without one there is nothing to run on EC2."
    if dockerfile:
        checks.append(_check("dockerfile", "Dockerfile", STATUS_PASS, "Found {}.".format(_rel(dockerfile, repo)), docker_why))
    else:
        checks.append(
            _check(
                "dockerfile",
                "Dockerfile",
                STATUS_FAIL,
                "No Dockerfile found.",
                docker_why,
                auto_fixable=framework in SUPPORTED_FRAMEWORKS,
                file="Dockerfile",
            )
        )

    # 8. .dockerignore ------------------------------------------------------ #
    ignore_why = "Keeps node_modules, .git and local secrets out of the build context: faster uploads, smaller images, no leaked .env files."
    if (app_dir / ".dockerignore").is_file() or (repo / ".dockerignore").is_file():
        checks.append(_check("dockerignore", ".dockerignore", STATUS_PASS, ".dockerignore found.", ignore_why))
    else:
        checks.append(
            _check("dockerignore", ".dockerignore", STATUS_WARN, "No .dockerignore found.", ignore_why, auto_fixable=True, file=".dockerignore")
        )

    # 9. lockfile ----------------------------------------------------------- #
    lock_why = "A lockfile pins exact dependency versions so the build is reproducible and `npm ci` can be used."
    if is_static_site or pkg is None:
        checks.append(_check("lockfile", "Dependency lockfile", STATUS_SKIPPED, "Not applicable.", lock_why))
    elif lockfile or any((repo / n).is_file() for n, _ in LOCKFILES):
        checks.append(_check("lockfile", "Dependency lockfile", STATUS_PASS, "Using {} ({}).".format(package_manager, lockfile or "workspace root lockfile"), lock_why))
    else:
        checks.append(
            _check(
                "lockfile",
                "Dependency lockfile",
                STATUS_WARN,
                "No lockfile committed; builds will resolve fresh versions each time.",
                lock_why,
                details=["Run `npm install` locally and commit package-lock.json."],
            )
        )

    # 10. committed env files ----------------------------------------------- #
    secret_why = "Env files committed to git often contain real credentials that end up in images and history."
    committed = [name for name in COMMITTED_ENV_NAMES if (app_dir / name).is_file()]
    if committed:
        checks.append(
            _check(
                "committed_env",
                "Committed env files",
                STATUS_WARN,
                "Found committed {}. Make sure it holds no secrets.".format(", ".join(committed)),
                secret_why,
                details=["If it contains secrets: remove it from git, add it to .gitignore and rotate the keys."],
            )
        )
    else:
        checks.append(_check("committed_env", "Committed env files", STATUS_PASS, "No env files committed.", secret_why))

    # Summary --------------------------------------------------------------- #
    health_score = _score(checks)
    failing = [c for c in checks if c["status"] == STATUS_FAIL]
    warnings = [c for c in checks if c["status"] == STATUS_WARN]
    if any(not c["auto_fixable"] for c in failing):
        status = "blocked"
    elif failing:
        status = "needs_config"
    elif warnings:
        status = "ready_with_warnings"
    else:
        status = "ready"

    missing_files = [
        {
            "file": c["file"],
            "check_id": c["id"],
            "severity": c["status"],
            "reason": c["why"],
            "message": c["message"],
            "auto_fixable": c["auto_fixable"],
            "manual_allowed": c["manual_allowed"],
        }
        for c in checks
        if c["status"] in (STATUS_FAIL, STATUS_WARN) and c["file"]
    ]

    public_prefixes = PUBLIC_ENV_PREFIXES.get(framework, ())
    return {
        "status": status,
        "health_score": health_score,
        "config_needed": 100 - health_score,
        "detected_framework": framework,
        "framework_label": framework_label,
        "app_root": _rel(app_dir, repo) or ".",
        "package_manager": package_manager,
        "lockfile": lockfile,
        "node_version": _detect_node_major(app_dir, pkg),
        "output_dir": output["output_dir"],
        "runtime": output["runtime"],
        "next_mode": output["next_mode"],
        "container_port": 3000 if output["runtime"] == "node" else 80,
        "env_keys": used_keys,
        "public_env_keys": [k for k in used_keys if public_prefixes and k.startswith(public_prefixes)],
        "has_client_router": _has_client_router(deps),
        "checks": checks,
        "missing_files": missing_files,
        "auto_fixable_items": [c["id"] for c in checks if c["auto_fixable"]],
        "manual_items": [c["file"] for c in checks if c["status"] in (STATUS_FAIL, STATUS_WARN) and c["manual_allowed"]],
        "inspected_at": datetime.now(timezone.utc).isoformat(),
    }


def build_generation_context(repo_path: str, report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Collect the repo facts the config templates need (from a fresh or given report)."""
    report = report or inspect_frontend_repo(repo_path)
    repo = Path(repo_path).resolve()
    app_dir = (repo / report["app_root"]).resolve()
    pkg, _ = _load_package_json(app_dir)
    template_path = app_dir / ".env.example"
    nginx_spa = [p for p in _find_nginx_configs(app_dir) if "try_files" in (_read_text(p) or "")]
    nginx_any = _find_nginx_configs(app_dir)
    nginx_path = (nginx_spa or nginx_any or [None])[0]
    return {
        "package_manager": report["package_manager"],
        "lockfile": report["lockfile"],
        "node_version": report["node_version"],
        "output_dir": report["output_dir"],
        "next_mode": report["next_mode"],
        "env_keys": report["env_keys"],
        "public_env_keys": report["public_env_keys"],
        "package_json": pkg,
        "existing_env_template": _read_text(template_path) if template_path.is_file() else None,
        "nginx_conf_path": _rel(nginx_path, app_dir) if nginx_path else None,
        "has_public_dir": (app_dir / "public").is_dir(),
        "has_yarnrc": (app_dir / ".yarnrc.yml").is_file(),
    }


# --------------------------------------------------------------------------- #
# Config generation
# --------------------------------------------------------------------------- #


def _install_command(manager: str, lockfile: Optional[str], has_yarnrc: bool) -> str:
    if manager == "pnpm":
        return "corepack enable && pnpm install " + ("--frozen-lockfile" if lockfile else "--no-frozen-lockfile")
    if manager == "yarn":
        if has_yarnrc:
            return "corepack enable && yarn install " + ("--immutable" if lockfile else "")
        return "corepack enable && yarn install " + ("--frozen-lockfile" if lockfile else "")
    if manager == "bun":
        return "npm install -g bun && bun install " + ("--frozen-lockfile" if lockfile else "")
    return "npm ci" if lockfile else "npm install"


def _run_build_command(manager: str) -> str:
    if manager == "pnpm":
        return "corepack enable && pnpm run build"
    if manager == "yarn":
        return "corepack enable && yarn run build"
    if manager == "bun":
        return "npm install -g bun && bun run build"
    return "npm run build"


def _manifest_copy_line(ctx: Dict[str, Any]) -> str:
    files = ["package.json"]
    if ctx.get("lockfile"):
        files.append(ctx["lockfile"])
    if ctx.get("has_yarnrc"):
        files.append(".yarnrc.yml")
    return "COPY {} ./".format(" ".join(files))


def _build_args_block(keys: List[str]) -> str:
    if not keys:
        return ""
    lines = ["# Public env vars are inlined at build time; pass them with --build-arg."]
    for key in keys:
        lines.append("ARG {}".format(key))
    lines.append("ENV " + " \\\n    ".join("{0}=${0}".format(k) for k in keys))
    return "\n".join(lines) + "\n"


def _generate_dockerfile(framework: str, ctx: Dict[str, Any]) -> Tuple[str, List[str]]:
    notes = []  # type: List[str]
    node_image = "node:{}-alpine".format(ctx.get("node_version") or DEFAULT_NODE_MAJOR)
    manager = ctx.get("package_manager") or "npm"
    install = _install_command(manager, ctx.get("lockfile"), bool(ctx.get("has_yarnrc"))).strip()
    build = _run_build_command(manager)
    header = "# syntax=docker/dockerfile:1\n# Generated by OpsMind pre-flight inspector (framework: {})\n".format(framework)

    if framework == FRAMEWORK_STATIC:
        conf = ctx.get("nginx_conf_path") or "nginx.conf"
        return (
            header
            + "\nFROM {image} AS runtime\n"
            "COPY {conf} /etc/nginx/conf.d/default.conf\n"
            "COPY . /usr/share/nginx/html\n"
            "RUN rm -f /usr/share/nginx/html/Dockerfile /usr/share/nginx/html/.dockerignore /usr/share/nginx/html/{conf}\n"
            "EXPOSE 80\n"
            "HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \\\n"
            "  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1\n"
        ).format(image=NGINX_IMAGE, conf=conf), notes

    deps_stage = (
        "\n# ---- 1. dependencies (cached until the manifest changes) ----\n"
        "FROM {node} AS deps\n"
        "RUN apk add --no-cache libc6-compat\n"
        "WORKDIR /app\n"
        "{copy}\n"
        "RUN {install}\n"
    ).format(node=node_image, copy=_manifest_copy_line(ctx), install=install)

    is_next = framework == FRAMEWORK_NEXT
    build_stage = (
        "\n# ---- 2. build ----\n"
        "FROM {node} AS build\n"
        "WORKDIR /app\n"
        "{telemetry}"
        "{args}"
        "COPY --from=deps /app/node_modules ./node_modules\n"
        "COPY . .\n"
        "RUN {build}\n"
    ).format(
        node=node_image,
        telemetry="ENV NEXT_TELEMETRY_DISABLED=1\n" if is_next else "",
        args=_build_args_block(ctx.get("public_env_keys") or []),
        build=build,
    )

    if is_next and ctx.get("next_mode") in ("server", "standalone"):
        dist = ctx.get("output_dir") or ".next"
        runtime = (
            "\n# ---- 3. runtime ----\n"
            "FROM {node} AS runtime\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production \\\n"
            "    NEXT_TELEMETRY_DISABLED=1 \\\n"
            "    PORT=3000 \\\n"
            "    HOSTNAME=0.0.0.0\n"
            "RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs\n"
        ).format(node=node_image)
        if ctx.get("next_mode") == "standalone":
            if ctx.get("has_public_dir"):
                runtime += "COPY --from=build --chown=nextjs:nodejs /app/public ./public\n"
            runtime += (
                "COPY --from=build --chown=nextjs:nodejs /app/{dist}/standalone ./\n"
                "COPY --from=build --chown=nextjs:nodejs /app/{dist}/static ./{dist}/static\n"
                "USER nextjs\n"
                "EXPOSE 3000\n"
                "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\\n"
                "  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1\n"
                'CMD ["node", "server.js"]\n'
            ).format(dist=dist)
        else:
            notes.append(
                "Next.js is not using output: 'standalone'. The image ships full node_modules. "
                "Add `output: \"standalone\"` to next.config for a much smaller image."
            )
            runtime += (
                "COPY --from=build --chown=nextjs:nodejs /app ./\n"
                "USER nextjs\n"
                "EXPOSE 3000\n"
                "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\\n"
                "  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1\n"
                'CMD ["node_modules/.bin/next", "start", "-p", "3000", "-H", "0.0.0.0"]\n'
            )
        return header + deps_stage + build_stage + runtime, notes

    # Static bundle (Vite, CRA, Vue CLI, Next export) served by nginx.
    out_dir = ctx.get("output_dir") or DEFAULT_OUTPUT_DIRS.get(framework, "dist")
    conf = ctx.get("nginx_conf_path") or "nginx.conf"
    runtime = (
        "\n# ---- 3. runtime: static files behind nginx ----\n"
        "FROM {nginx} AS runtime\n"
        "COPY {conf} /etc/nginx/conf.d/default.conf\n"
        "COPY --from=build /app/{out} /usr/share/nginx/html\n"
        "EXPOSE 80\n"
        "HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \\\n"
        "  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1\n"
    ).format(nginx=NGINX_IMAGE, conf=conf, out=out_dir.strip("/"))
    return header + deps_stage + build_stage + runtime, notes


def _nginx_security_headers(indent: str) -> str:
    headers = (
        'add_header X-Content-Type-Options "nosniff" always;',
        'add_header X-Frame-Options "SAMEORIGIN" always;',
        'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
    )
    return "".join(indent + h + "\n" for h in headers)


def _generate_nginx_conf(framework: str, ctx: Dict[str, Any]) -> str:
    """nginx server block for the static runtime. Headers are repeated per location
    because nginx drops inherited add_header directives once a location sets its own."""
    spa = framework in (FRAMEWORK_VITE, FRAMEWORK_CRA, FRAMEWORK_VUE_CLI)
    fallback = "try_files $uri $uri/ /index.html;" if spa else "try_files $uri $uri.html $uri/ =404;"
    hashed = HASHED_ASSET_PREFIXES.get(framework, [])

    lines = [
        "# Generated by OpsMind pre-flight inspector (framework: {})".format(framework),
        "server {",
        # IPv4 only: `listen [::]:80` makes nginx exit on hosts with IPv6 disabled
        # (common for Docker on EC2).
        "    listen 80;",
        "    server_name _;",
        "    root /usr/share/nginx/html;",
        "    index index.html;",
        "",
        "    server_tokens off;",
        "    gzip on;",
        "    gzip_vary on;",
        "    gzip_min_length 1024;",
        "    gzip_types text/plain text/css text/javascript application/javascript application/json application/xml image/svg+xml;",
        "",
        _nginx_security_headers("    ").rstrip("\n"),
        "",
        "    # Liveness probe for OpsMind / load balancers",
        "    location = /healthz {",
        "        access_log off;",
        "        default_type text/plain;",
        '        return 200 "ok\\n";',
        "    }",
    ]
    for prefix in hashed:
        lines += [
            "",
            "    # Content-hashed build assets: safe to cache for a year",
            "    location ^~ {} {{".format(prefix),
            "        try_files $uri =404;",
            '        add_header Cache-Control "public, max-age=31536000, immutable";',
            _nginx_security_headers("        ").rstrip("\n"),
            "    }",
        ]
    lines += [
        "",
        "    # Never cache the HTML shell so a new deploy is picked up immediately",
        "    location ~* \\.html$ {",
        '        add_header Cache-Control "no-cache";',
        _nginx_security_headers("        ").rstrip("\n"),
        "    }",
        "",
        "    location / {",
        "        " + fallback,
        "    }",
    ]
    if not spa:
        lines += ["", "    error_page 404 /404.html;"]
    lines.append("}")
    return "\n".join(lines) + "\n"


def _generate_env_example(framework: str, ctx: Dict[str, Any]) -> str:
    keys = list(ctx.get("env_keys") or [])
    prefixes = PUBLIC_ENV_PREFIXES.get(framework, ())
    existing = ctx.get("existing_env_template")

    if existing is not None:
        missing = sorted(set(keys) - _parse_env_keys(existing))
        if not missing:
            return existing
        body = existing if existing.endswith("\n") else existing + "\n"
        body += "\n# Added by OpsMind pre-flight: referenced in code but undocumented\n"
        body += "".join("{}=\n".format(k) for k in missing)
        return body

    public = [k for k in keys if prefixes and k.startswith(prefixes)]
    private = [k for k in keys if k not in public]
    out = [
        "# Environment variables referenced by this app.",
        "# Copy to .env (never commit real values) and fill in.",
        "# Generated by OpsMind pre-flight inspector.",
    ]
    if public:
        out += ["", "# Public: inlined into the browser bundle at BUILD time. Never put secrets here."]
        out += ["{}=".format(k) for k in public]
    if private:
        out += ["", "# Server / build-time only"]
        out += ["{}=".format(k) for k in private]
    return "\n".join(out) + "\n"


def _generate_dockerignore(framework: str, ctx: Dict[str, Any]) -> str:
    lines = [
        "# Generated by OpsMind pre-flight inspector",
        "node_modules",
        "**/node_modules",
        ".git",
        ".gitignore",
        ".github",
        ".vscode",
        ".idea",
        "coverage",
        ".turbo",
        ".cache",
        "*.log",
        ".DS_Store",
        "",
        "# Local secrets never belong in the image",
        ".env.local",
        ".env.*.local",
    ]
    if framework != FRAMEWORK_STATIC:
        lines += ["", "# Build output is produced inside the image", ".next", "dist", "build", "out"]
        out_dir = (ctx.get("output_dir") or "").strip("/")
        if out_dir and out_dir not in (".next", "dist", "build", "out", "."):
            lines.append(out_dir)
    return "\n".join(lines) + "\n"


def _generate_package_json(framework: str, ctx: Dict[str, Any]) -> Optional[str]:
    pkg = ctx.get("package_json")
    command = DEFAULT_BUILD_SCRIPTS.get(framework)
    if not isinstance(pkg, dict) or not command:
        return None
    patched = dict(pkg)
    scripts = dict(patched.get("scripts") or {})
    scripts["build"] = command
    if framework == FRAMEWORK_NEXT and "start" not in scripts:
        scripts["start"] = "next start"
    patched["scripts"] = scripts
    return json.dumps(patched, indent=2, ensure_ascii=False) + "\n"


def _normalise_item(item: str) -> Optional[str]:
    if item in FIX_TARGETS:
        return item
    return ITEM_ALIASES.get(item) or ITEM_ALIASES.get(item.lower())


def generate_missing_configs(missing_items: List[str], framework: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Generate file contents for the requested fixes.

    ``missing_items`` accepts check ids (``dockerfile``, ``spa_routing``,
    ``env_template``, ``dockerignore``, ``build_script``) or file names
    (``Dockerfile``, ``nginx.conf``...). ``context`` comes from
    ``build_generation_context`` and carries repo facts; without it sensible
    defaults (npm, no lockfile, Node 22) are used.

    Returns ``{"framework", "files": [{path, content, check_id, description}],
    "skipped": [{item, reason}], "notes": [...], "container_port"}``.
    """
    ctx = dict(context or {})
    if framework not in SUPPORTED_FRAMEWORKS:
        return {
            "framework": framework,
            "files": [],
            "skipped": [{"item": i, "reason": "Framework \"{}\" is not supported for auto-generation.".format(framework)} for i in missing_items],
            "notes": [],
            "container_port": None,
        }

    requested = []  # type: List[str]
    skipped = []  # type: List[Dict[str, str]]
    for item in missing_items:
        check_id = _normalise_item(item)
        if check_id is None:
            skipped.append({"item": item, "reason": "Unknown or non auto-fixable item."})
        elif check_id not in requested:
            requested.append(check_id)

    runtime_static = framework != FRAMEWORK_NEXT or ctx.get("next_mode") == "export"
    if "spa_routing" in requested:
        # The Dockerfile must COPY the config we are about to generate.
        ctx["nginx_conf_path"] = "nginx.conf"
    # A generated nginx Dockerfile COPYs nginx.conf, so make sure one exists.
    if "dockerfile" in requested and runtime_static and not ctx.get("nginx_conf_path") and "spa_routing" not in requested:
        requested.append("spa_routing")

    files = []  # type: List[Dict[str, str]]
    notes = []  # type: List[str]
    for check_id in requested:
        if check_id == "dockerfile":
            content, extra = _generate_dockerfile(framework, ctx)
            notes.extend(extra)
            files.append({"path": "Dockerfile", "content": content, "check_id": check_id, "description": "Multi-stage production Dockerfile"})
        elif check_id == "spa_routing":
            if not runtime_static:
                skipped.append({"item": check_id, "reason": "Next.js server mode does not need nginx."})
                continue
            files.append({"path": "nginx.conf", "content": _generate_nginx_conf(framework, ctx), "check_id": check_id, "description": "nginx config with SPA fallback, caching and gzip"})
        elif check_id == "env_template":
            if not ctx.get("env_keys"):
                skipped.append({"item": check_id, "reason": "No env keys detected in code."})
                continue
            files.append({"path": ".env.example", "content": _generate_env_example(framework, ctx), "check_id": check_id, "description": "Template of every env key the code reads"})
        elif check_id == "dockerignore":
            files.append({"path": ".dockerignore", "content": _generate_dockerignore(framework, ctx), "check_id": check_id, "description": "Keeps node_modules, .git and local secrets out of the build"})
        elif check_id == "build_script":
            content = _generate_package_json(framework, ctx)
            if content is None:
                skipped.append({"item": check_id, "reason": "package.json missing or framework has no default build command."})
                continue
            files.append({"path": "package.json", "content": content, "check_id": check_id, "description": "Adds the \"build\" script for {}".format(framework)})

    if ctx.get("public_env_keys") and "dockerfile" in requested:
        notes.append("Pass public env vars at build time: docker build --build-arg {}=... .".format(ctx["public_env_keys"][0]))

    return {
        "framework": framework,
        "files": files,
        "skipped": skipped,
        "notes": notes,
        "container_port": 80 if runtime_static else 3000,
    }


# --------------------------------------------------------------------------- #
# Writing files back into the repo
# --------------------------------------------------------------------------- #


def safe_target(app_dir: Path, relative: str) -> Path:
    """Resolve ``relative`` inside ``app_dir`` and refuse anything that escapes it."""
    if not relative or relative.startswith(("/", "\\")) or "\\" in relative or ".." in Path(relative).parts:
        raise ValueError("Invalid target path: {}".format(relative))
    base = app_dir.resolve()
    target = base / relative
    parent = target.parent.resolve()
    if parent != base and base not in parent.parents:
        raise ValueError("Target escapes the repository: {}".format(relative))
    return parent / target.name


def write_files(app_dir: Path, files: List[Dict[str, str]], overwrite: bool = False) -> Dict[str, List[Dict[str, str]]]:
    """
    Atomically write generated or user-supplied files into the app directory.

    Existing files are kept unless ``overwrite`` is true, except package.json,
    which only ever arrives here as a patched copy of the existing file.
    """
    written = []  # type: List[Dict[str, str]]
    skipped = []  # type: List[Dict[str, str]]
    for item in files:
        rel = item["path"]
        try:
            target = safe_target(app_dir, rel)
        except ValueError as exc:
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        if target.exists() and not overwrite and rel != "package.json":
            skipped.append({"path": rel, "reason": "File already exists (enable overwrite to replace it)."})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".opsmind-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(item["content"])
            os.chmod(tmp, 0o644)
            # os.replace swaps the directory entry, so a symlinked target is
            # replaced rather than followed.
            os.replace(tmp, str(target))
        except OSError as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            skipped.append({"path": rel, "reason": "Write failed: {}".format(exc)})
            continue
        written.append({"path": rel, "check_id": item.get("check_id", "manual")})
    return {"written": written, "skipped": skipped}
