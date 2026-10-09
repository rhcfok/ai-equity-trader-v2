# -*- coding: utf-8 -*-
"""Capture TradingView charts for J Law visual review via Kimi WebBridge.

Usage:
    python tv_capture.py --symbols AAPL:NASDAQ,XOM:NYSE,V:NYSE --out <dir>
    python tv_capture.py --from-checklist out/jlaw_checklist_YYYY-MM-DD.json --min-score 10 --out <dir>
    python tv_capture.py --from-watchlist --out <dir>

Notes:
- --from-watchlist pulls the chart pipeline's universe straight from the
  Supabase `watchlist` table (category in core/satellite/watch1) using
  SUPABASE_URL / SUPABASE_KEY; the table's `exchange` column is used for
  the TradingView symbol URL. This is the standard full-run mode.
- Exchange defaults to NYSE when unknown; most NASDAQ names are resolved
  from the watchlist table or EXCH_MAP below.
- TradingView chart URL: https://www.tradingview.com/chart/<layout_id>/?symbol=EXCH%3ASYM
  Override layout with --layout (default is the user's "Kimi" layout).
- Blank/black screenshots mean the tab was backgrounded: the script sends
  Page.bringToFront before every shot, which fixes it without stealing focus.
"""
import argparse, json, os, sys, time, urllib.request

DAEMON = "http://127.0.0.1:10086/command"
SESSION = "jlaw-tv-chart"
DEFAULT_LAYOUT = "hnR03PEX"  # user's customised "Kimi" layout
WATCHLIST_CATEGORIES = ("core", "satellite", "watch1")

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


def from_watchlist():
    """Fetch (symbol, exchange) for the chart pipeline's universe from
    Supabase: watchlist rows whose category is core/satellite/watch1."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        print(json.dumps({"ok": False,
                          "error": "SUPABASE_URL / SUPABASE_KEY not set"}))
        sys.exit(2)
    if not url.endswith("/rest/v1"):
        url += "/rest/v1"
    cats = ",".join(f'"{c}"' for c in WATCHLIST_CATEGORIES)
    req = urllib.request.Request(
        f"{url}/watchlist?select=symbol,exchange&category=in.({cats})",
        headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.loads(r.read().decode("utf-8"))
    out = []
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        exch = (row.get("exchange") or "").strip().upper() or exchange_for(sym)
        out.append((sym, exch))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma list, e.g. AAPL:NASDAQ,XOM:NYSE")
    ap.add_argument("--from-checklist", help="jlaw checklist JSON path")
    ap.add_argument("--from-watchlist", action="store_true",
                    help="full chart universe from Supabase watchlist "
                         "(core/satellite/watch1)")
    ap.add_argument("--min-score", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--layout", default=DEFAULT_LAYOUT)
    ap.add_argument("--wait", type=float, default=9.0)
    args = ap.parse_args()

    if args.from_watchlist:
        syms = from_watchlist()
    elif args.from_checklist:
        syms = from_checklist(args.from_checklist, args.min_score)
    else:
        syms = parse_symbols(args.symbols)
    print(f"capturing {len(syms)} symbols", flush=True)
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
