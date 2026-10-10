"""
Pre-flight API: inspect an ingested frontend repo and inject missing configs.

Both endpoints are plain ``def`` (not ``async def``) on purpose: they do
blocking filesystem work, so FastAPI runs them in its threadpool instead of
stalling the event loop.
"""

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.frontend_inspector import (
    FIX_TARGETS,
    MANUAL_ALLOWED_FILES,
    InspectorError,
    build_generation_context,
    generate_missing_configs,
    inspect_frontend_repo,
    write_files,
)

router = APIRouter(prefix="/api", tags=["preflight"])

# Every repo path coming from the client must live under this directory.
WORKSPACE_ROOT = Path(os.getenv("OPSMIND_WORKSPACE_ROOT", "/var/opsmind/workspaces")).resolve()
MAX_MANUAL_FILE_BYTES = 64 * 1024

_repo_locks = {}  # type: Dict[str, threading.Lock]
_repo_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _repo_locks_guard:
        return _repo_locks.setdefault(str(path), threading.Lock())


def resolve_workspace_path(repo_path: str) -> Path:
    """Map a client-supplied path (absolute or relative) into the workspace root, rejecting escapes."""
    candidate = Path(repo_path)
    if not candidate.is_absolute():
        candidate = WORKSPACE_ROOT / candidate
    resolved = candidate.resolve()
    if resolved != WORKSPACE_ROOT and WORKSPACE_ROOT not in resolved.parents:
        raise HTTPException(status_code=400, detail="repo_path must be inside the OpsMind workspace.")
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Repository not found in workspace.")
    return resolved


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class InspectRequest(BaseModel):
    repo_path: str = Field(..., min_length=1, max_length=1024, description="Workspace-relative or absolute path of the ingested repo")


class ManualFile(BaseModel):
    path: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., max_length=MAX_MANUAL_FILE_BYTES)


class InjectRequest(BaseModel):
    repo_path: str = Field(..., min_length=1, max_length=1024)
    mode: Literal["auto", "manual"] = "auto"
    # auto mode: check ids to fix. Omit to fix everything the inspector marked auto-fixable.
    items: Optional[List[str]] = None
    # manual mode: user-supplied file contents.
    files: Optional[List[ManualFile]] = None
    overwrite: bool = False
    # Return generated contents without writing anything (used for the preview).
    dry_run: bool = False


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post("/inspect/frontend")
def inspect_frontend(req: InspectRequest) -> Dict[str, Any]:
    repo = resolve_workspace_path(req.repo_path)
    try:
        return inspect_frontend_repo(str(repo))
    except InspectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/autofix/inject")
def autofix_inject(req: InjectRequest) -> Dict[str, Any]:
    repo = resolve_workspace_path(req.repo_path)

    with _lock_for(repo):
        try:
            # Always re-inspect server side; never trust framework/paths sent by the client.
            report = inspect_frontend_repo(str(repo))
        except InspectorError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        app_dir = (repo / report["app_root"]).resolve()

        if req.mode == "auto":
            fixable = set(report["auto_fixable_items"])
            requested = req.items if req.items is not None else sorted(fixable)
            unknown = [i for i in requested if i not in FIX_TARGETS]
            if unknown:
                raise HTTPException(status_code=400, detail="Unknown fix items: {}".format(", ".join(unknown)))
            not_needed = [i for i in requested if i not in fixable]
            todo = [i for i in requested if i in fixable]
            context = build_generation_context(str(repo), report)
            generated = generate_missing_configs(todo, report["detected_framework"], context)
            files = generated["files"]
            skipped = generated["skipped"] + [{"item": i, "reason": "Check is already passing or not auto-fixable."} for i in not_needed]
            notes = generated["notes"]
        else:
            if not req.files:
                raise HTTPException(status_code=400, detail="Manual mode requires at least one file.")
            bad = [f.path for f in req.files if f.path not in MANUAL_ALLOWED_FILES]
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail="Only these files can be uploaded: {}. Rejected: {}".format(", ".join(MANUAL_ALLOWED_FILES), ", ".join(bad)),
                )
            files = [{"path": f.path, "content": f.content, "check_id": "manual", "description": "Provided by user"} for f in req.files]
            skipped, notes = [], []

        if req.dry_run:
            return {"dry_run": True, "files": files, "written": [], "skipped": skipped, "notes": notes, "report": report}

        result = write_files(app_dir, files, overwrite=req.overwrite)
        new_report = inspect_frontend_repo(str(repo))
        return {
            "dry_run": False,
            "files": [{"path": f["path"], "check_id": f.get("check_id"), "description": f.get("description")} for f in files],
            "written": result["written"],
            "skipped": skipped + [{"item": s["path"], "reason": s["reason"]} for s in result["skipped"]],
            "notes": notes,
            "report": new_report,
        }
