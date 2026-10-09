# -*- coding: utf-8 -*-
"""Capture TradingView charts for J Law visual review via Kimi WebBridge.

Usage:
    python tv_capture.py --symbols AAPL:NASDAQ,XOM:NYSE,V:NYSE --out <dir>
    python tv_capture.py --from-checklist out/jlaw_checklist_YYYY-MM-DD.json --min-score 10 --out <dir>

Notes:
- Exchange defaults to NYSE; most NASDAQ names must be given explicitly.
- TradingView chart URL: https://www.tradingview.com/chart/<layout_id>/?symbol=EXCH%3ASYM
  Override layout with --layout (default is the user's "Kimi" layout).
- Blank/black screenshots mean the tab was backgrounded: the script sends
  Page.bringToFront before every shot, which fixes it without stealing focus.
"""
import argparse, json, os, time, urllib.request

DAEMON = "http://127.0.0.1:10086/command"
SESSION = "jlaw-tv-chart"
DEFAULT_LAYOUT = "hnR03PEX"  # user's customised "Kimi" layout

EXCH_MAP = {  # extend as needed; default NYSE
    "NASDAQ": {"AAPL", "DUOL", "NXPI", "MU", "MRVL", "AMZN", "COHR", "LITE",
               "DOCN", "NOW", "GLW", "INTC", "AMAT"},
}


def cmd(action, args=None, timeout=60):
    body = json.dumps({"action": action, "args": args or {},
                       "session": SESSION}).encode("utf-8")
    req = urllib.request.Request(DAEMON, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def exchange_for(sym):
    for exch, names in EXCH_MAP.items():
        if sym in names:
            return exch
    return "NYSE"


def parse_symbols(spec):
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            sym, exch = tok.split(":", 1)
            out.append((sym.strip().upper(), exch.strip().upper()))
        else:
            out.append((tok.upper(), exchange_for(tok.upper())))
    return out


def from_checklist(path, min_score):
    d = json.load(open(path, encoding="utf-8"))
    rows = d.get("candidates", [])
    return [(r["symbol"], exchange_for(r["symbol"]))
            for r in rows if r.get("jlaw_score", 0) >= min_score]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma list, e.g. AAPL:NASDAQ,XOM:NYSE")
    ap.add_argument("--from-checklist", help="jlaw checklist JSON path")
    ap.add_argument("--min-score", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--layout", default=DEFAULT_LAYOUT)
    ap.add_argument("--wait", type=float, default=9.0)
    args = ap.parse_args()

    if args.from_checklist:
        syms = from_checklist(args.from_checklist, args.min_score)
    else:
        syms = parse_symbols(args.symbols)
    os.makedirs(args.out, exist_ok=True)

    for sym, exch in syms:
        url = (f"https://www.tradingview.com/chart/{args.layout}/"
               f"?symbol={exch}%3A{sym}")
        cmd("navigate", {"url": url, "newTab": False})
        time.sleep(args.wait)
        cmd("cdp", {"method": "Page.bringToFront", "params": {}})
        time.sleep(3)
        r = cmd("screenshot", {"format": "jpeg", "quality": 68,
                               "path": os.path.join(args.out, f"{sym}.jpg")})
        print(sym, r.get("ok"), (r.get("data") or {}).get("sizeBytes"),
              flush=True)
    print("DONE")


if __name__ == "__main__":
    main()
