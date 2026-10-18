#!/usr/bin/env python3
"""Apply this skill's SQL migrations to Supabase via the Management API.

Requires a Supabase personal access token (``SUPABASE_ACCESS_TOKEN``, e.g. a
scoped PAT with database read-write for the project). The project ref is
derived from ``SUPABASE_URL``. Idempotent: each ``.sql`` file's SHA-256 is
recorded in a local state file and skipped on re-runs, so the migration
directory can also be applied by hand (SQL editor) without breaking future
runs of this tool.

The access token is read from the environment or ``--dotenv`` and is never
printed or persisted by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DEFAULT_CACHE_DIR = Path("uw_unusual_option_flow_cache")
DEFAULT_STATE = DEFAULT_CACHE_DIR / "migrations_applied.json"
API_BASE = "https://api.supabase.com"


class MigrationError(RuntimeError):
    """Raised for invalid configuration or failed queries."""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def project_ref(supabase_url: str) -> str:
    """Extract the project ref from a Supabase URL (hostname's first label)."""
    host = supabase_url.split("//", 1)[-1].split("/", 1)[0]
    ref = host.split(".")[0]
    if not ref or not host.endswith("supabase.co"):
        raise MigrationError(f"cannot derive project ref from SUPABASE_URL (got {host!r})")
    return ref


def load_state(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_query(session: requests.Session, token: str, ref: str, query: str) -> None:
    response = session.post(
        f"{API_BASE}/v1/projects/{ref}/database/query",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query},
        timeout=120,
    )
    if not response.ok:
        raise MigrationError(f"query failed ({response.status_code}): {response.text[:400]}")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(args.dotenv)
    token = optional_env("SUPABASE_ACCESS_TOKEN")
    if not token:
        raise MigrationError("SUPABASE_ACCESS_TOKEN not set (env or --dotenv)")
    ref = args.project_ref or project_ref(optional_env("SUPABASE_URL"))
    state_path = Path(args.state_file)
    applied = load_state(state_path)
    session = requests.Session()
    results: list[dict[str, str]] = []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise MigrationError(f"no .sql migrations under {MIGRATIONS_DIR}")
    for sql_file in files:
        digest = hashlib.sha256(sql_file.read_bytes()).hexdigest()
        if applied.get(sql_file.name) == digest:
            results.append({"file": sql_file.name, "status": "skipped"})
            continue
        if args.dry_run:
            results.append({"file": sql_file.name, "status": "dry-run"})
            continue
        run_query(session, token, ref, sql_file.read_text(encoding="utf-8"))
        applied[sql_file.name] = digest
        save_state(state_path, applied)
        results.append({"file": sql_file.name, "status": "applied"})
        logging.info("applied %s", sql_file.name)
    return {"ok": True, "project_ref": ref, "results": results}


def self_test() -> dict[str, Any]:
    checks = 0
    assert project_ref("https://vmxxmdtzvwizrpjrrvnp.supabase.co") == "vmxxmdtzvwizrpjrrvnp"
    checks += 1
    assert project_ref("https://abc.supabase.co/rest/v1/") == "abc"
    checks += 1
    assert project_ref("https://abc.supabase.co") == "abc"
    checks += 1
    for bad in ("", "not-a-url", "https://abc.example.com"):
        try:
            project_ref(bad)
            raised = False
        except MigrationError:
            raised = True
        assert raised
        checks += 1
    state = load_state(Path("__missing_state__.json"))
    assert state == {}
    checks += 1
    return {"ok": True, "checks": checks}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply uw-unusual-option-flow SQL migrations via the Supabase Management API.")
    parser.add_argument("--dotenv", type=Path, default=Path(".env"), help="private env file")
    parser.add_argument("--project-ref", default="", help="override the project ref derived from SUPABASE_URL")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE), help="local record of applied migration SHAs")
    parser.add_argument("--dry-run", action="store_true", help="list pending migrations without executing")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--self-test", action="store_true", help="run offline deterministic checks")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False))
        return 0
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), stream=os.sys.stderr)
    try:
        result = execute(args)
    except MigrationError as exc:
        logging.error("migration failed: %s", exc)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
