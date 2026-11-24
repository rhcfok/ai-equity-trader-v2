"""Unusual Whales short-put screen for the equity-fv-short-put skill.

Usage:
    python -I uw_put_screen.py --config names.json --out uw_out
    python -I uw_put_screen.py --config names.json --expiries 2026-12-18,2027-01-15

names.json: {"MSFT": 505, "GOOGL": 280, ...}

The script reads UW_API_KEY from an env file and never prints it. If the key or the
endpoint is unavailable it exits non-zero rather than emitting placeholder quotes.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

MCP_URL = "https://api.unusualwhales.com/api/mcp"
DEFAULT_ENV_PATHS = [
    os.environ.get("UW_ENV_FILE", ""),
    r"D:\AI projects\AI-trading\ai-equity-trader-v2\.env",
    os.path.expanduser("~/.env"),
    ".env",
]


def load_key():
    for raw in DEFAULT_ENV_PATHS:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("UW_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    return None


def post(body, key, session=None, timeout=180):
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("UW-CLIENT-API-ID", "100001")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session:
        req.add_header("mcp-session-id", session)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        sid = resp.headers.get("mcp-session-id") or session
    lines = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
    return (json.loads(lines[-1]) if lines else json.loads(raw)), sid


def call(name, args, key, session, cid):
    payload, session = post({"jsonrpc": "2.0", "id": cid, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, key, session)
    result = payload.get("result", {})
    if result.get("isError"):
        return None, session
    sc = result.get("structuredContent")
    if sc is not None:
        return sc, session
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"]), session
            except Exception:
                return item["text"], session
    return result, session


def fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="JSON file mapping TICKER -> fair value")
    ap.add_argument("--expiries", default="", help="comma-separated YYYY-MM-DD; default 4 monthlies")
    ap.add_argument("--out", default="uw_out")
    ap.add_argument("--band-low", type=float, default=0.72, help="low strike as fraction of FV")
    ap.add_argument("--band-high", type=float, default=0.95, help="high strike as fraction of FV")
    ap.add_argument("--spot-cap", type=float, default=0.995,
                    help="never screen strikes above this x spot (keeps puts OTM)")
    ap.add_argument("--min-oi", type=int, default=50)
    ap.add_argument("--today", default="", help="YYYY-MM-DD; defaults to system date")
    ap.add_argument("--top", type=int, default=6)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or not cfg:
        sys.exit("config must be a non-empty JSON object of TICKER -> FV")

    today = date.fromisoformat(args.today) if args.today else date.today()
    if args.expiries:
        expiries = [e.strip() for e in args.expiries.split(",") if e.strip()]
    else:
        expiries = []
        y, m = today.year, today.month
        while len(expiries) < 4:
            # third Friday of the next months
            y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
            y, m = y2, m2
            first = date(y, m, 1)
            offset = (4 - first.weekday()) % 7
            third = first.replace(day=1 + offset + 14)
            expiries.append(third.isoformat())

    key = load_key()
    if not key:
        sys.exit("BLOCKED: UW_API_KEY not found in any known env file")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    _, session = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "equity-fv-short-put", "version": "1.0"}}},
                      key)
    cid = 2
    summary = {}

    for ticker, fv in cfg.items():
        fv = float(fv)
        print(f"\n===== {ticker} | FV {fv:.2f} =====")
        uw_spot, edate = None, None
        try:
            info, session = call("get_company_info", {"ticker": ticker}, key, session, cid)
            cid += 1
            if info and info.get("price"):
                uw_spot = fnum(info["price"])
            earn, session = call("get_upcoming_earnings", {"ticker": ticker, "limit": 3}, key, session, cid)
            cid += 1
            if earn and earn.get("result"):
                edate = earn["result"][0].get("report_date")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  info/earnings failed: {exc}")
        print(f"  UW spot {uw_spot} | next earnings {edate}")

        rows_all = []
        for expiry in expiries:
            try:
                data, session = call("get_chains_for_expiry",
                                     {"ticker": ticker, "expiry": expiry, "limit": 1000},
                                     key, session, cid)
                cid += 1
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                print(f"  {expiry}: {exc}")
                time.sleep(1.5)
                continue
            if not data:
                continue
            rows = data.get("result", data.get("data", []))
            px = data.get("price_data", {}).get("price") if isinstance(data, dict) else None
            spot = fnum(px) or uw_spot
            if not spot:
                continue
            dte = (date.fromisoformat(expiry) - today).days
            if dte <= 0:
                continue
            lo = args.band_low * fv
            hi = min(args.band_high * fv, args.spot_cap * spot)
            cand = []
            for r in rows:
                if r.get("option_type") != "put":
                    continue
                k = fnum(r.get("strike"))
                if not (lo <= k <= hi):
                    continue
                oi = int(fnum(r.get("open_interest")))
                if oi < args.min_oi:
                    continue
                bid = fnum(r.get("nbbo_bid"))
                ask = fnum(r.get("nbbo_ask"))
                mid = (bid + ask) / 2 if bid and ask else fnum(r.get("last_price"))
                if mid <= 0:
                    continue
                sprd = (ask - bid) / mid * 100 if mid else 0.0
                static_bid = bid / k * 100
                static_mid = mid / k * 100
                cand.append(dict(
                    exp=expiry, dte=dte, k=k, bid=bid, ask=ask, mid=mid,
                    iv=fnum(r.get("iv")) * 100, delta=r.get("delta"), oi=oi,
                    vol=int(fnum(r.get("volume"))), spr=sprd,
                    static_bid=static_bid, static_mid=static_mid,
                    ann_bid=static_bid * 365 / dte, ann_mid=static_mid * 365 / dte,
                    be=k - bid, buf=(fv - k) / fv * 100,
                    buf_be=(fv - (k - bid)) / fv * 100,
                    otm=(spot - k) / spot * 100,
                ))
            cand.sort(key=lambda x: -x["ann_bid"])
            print(f"  -- {expiry} DTE {dte} spot {spot:.2f} band {lo:.0f}-{hi:.0f} --")
            hdr = (f"  {'K':>8}{'Bid':>7}{'Mid':>7}{'IV%':>6}{'Delta':>7}{'OI':>8}"
                   f"{'Spr%':>6}{'AnnBid':>7}{'AnnMid':>7}{'BE':>8}{'BufK':>6}{'BufBE':>6}{'OTM':>6}")
            print(hdr)
            for r in cand[:args.top]:
                d = r["delta"]
                print(f"  {r['k']:>8.1f}{r['bid']:>7.2f}{r['mid']:>7.2f}{r['iv']:>6.1f}"
                      f"{(d if d is not None else 0):>7.3f}{r['oi']:>8}{r['spr']:>6.1f}"
                      f"{r['ann_bid']:>7.1f}{r['ann_mid']:>7.1f}{r['be']:>8.1f}"
                      f"{r['buf']:>6.1f}{r['buf_be']:>6.1f}{r['otm']:>6.1f}")
            rows_all.extend(cand[:args.top])
            time.sleep(1.0)

        summary[ticker] = dict(spot=uw_spot, fv=fv, earnings=edate, rows=rows_all)
        (out / f"{ticker}.json").write_text(json.dumps(summary[ticker], indent=1), encoding="utf-8")

    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\nwrote {out}/summary.json  ({datetime.now():%Y-%m-%d %H:%M})")


if __name__ == "__main__":
    main()
