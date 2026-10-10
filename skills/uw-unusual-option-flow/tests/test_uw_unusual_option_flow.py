#!/usr/bin/env python3
"""Run the deterministic offline checks embedded in the UW unusual-flow runner."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "uw_unusual_option_flow.py"
spec = importlib.util.spec_from_file_location("uw_unusual_option_flow", RUNNER)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load runner module")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

if __name__ == "__main__":
    result = module.self_test()
    assert result["ok"] is True
    assert result["checks"] >= 10
    print(result)
