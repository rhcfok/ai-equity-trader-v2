#!/usr/bin/env python3
"""Run ORATS leaderboard pipeline scripts with credentials from the local .env.

Maps the repo's lowercase .env names to the uppercase names the pipeline
expects, without printing any secret values.
"""
import os
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"

NAME_MAP = {
    "supabase_url": "SUPABASE_URL",
    "supabase_service_role": "SUPABASE_KEY",
    "api_key": "ORATS_TOKEN",
}


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        target = NAME_MAP.get(key, key)
        env[target] = value
    return env


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_with_env.py <script> [args...]")
        return 2
    env = load_env()
    missing = [n for n in ("SUPABASE_URL", "SUPABASE_KEY", "ORATS_TOKEN") if not env.get(n)]
    if missing:
        print(f"MISSING_SECRETS: {missing}")
        return 2
    print(f"secrets loaded: SUPABASE_URL, SUPABASE_KEY, ORATS_TOKEN (from {ENV_FILE.name})")
    cmd = [sys.executable, *sys.argv[1:]]
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
