#!/usr/bin/env python3
"""
gex_workflow_runner.py — Compute GEX snapshots from ORATS and upsert to Supabase.

Symbol universe (resolved at startup):
  - If BRIEFING_SYMBOLS env is set, use it as an explicit override.
  - Otherwise, pull tickers from the Supabase watchlist table where
    category is in WATCHLIST_CATEGORIES (default: core, satellite, watch1).

For each resolved symbol:
  1) Hit ORATS delayed strikes for the configured DTE window
  2) Filter by minimum OI and moneyness (configurable)
  3) Aggregate per-strike gamma into unsigned call-side and put-side GEX magnitudes
  4) Identify call wall (largest call-side GEX with strike >= spot) and
     put wall (largest put-side GEX with strike <= spot). If no strike on the
     correct side meets the filter, the wall is null and the row is
     `walls_undefined` — the briefing must then go map-only.
  5) Compute net GEX (call-side minus put-side, both unsigned) in $MM.
     NO claim is made about dealer long/short positioning — see audit note
     in MEMORY.md. The signed-dealer-gamma claim requires a separate source
     (Cboe Open-Close, OCC volume-by-account) and is not produced here.
  6) Set regime = positive / negative / sub_resolution (if |net_gex_mm| < threshold).
  7) Carry `tradeDate` from the ORATS row into the snapshot; if it's a
     weekend/holiday, mark `validity="weekend_tradedate"`.

Stdlib only (urllib + json). No external deps.

Required env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ORATS_API_KEY
Optional env: SNAPSHOT_TABLE, BRIEFING_SYMBOLS (override), WATCHLIST_TABLE,
              WATCHLIST_CATEGORIES (default "core,satellite,watch1"),
              ORATS_BASE, ORATS_DTE_FILTER,
              BATCH_SIZE, REQUEST_TIMEOUT_SEC, RUN_LABEL,
              GEX_MIN_OI (default 50), GEX_MONEYNESS_PCT (default 20),
              GEX_RESOLUTION_MM (default 1.0)

Output contract:
  INFO lines per symbol (one per processed symbol, including failures).
  Final stdout line: {"ok": bool, "processed": int, "failed_count": int, "failed": [...]}
  Exit 0 on success (full or partial), non-zero on hard setup failure.
"""
import os
import sys
import json
import time
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

_raw_supabase = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
# Accept either project root or already-qualified /rest/v1 base.
if _raw_supabase.endswith("/rest/v1"):
    SUPABASE_URL = _raw_supabase[: -len("/rest/v1")].rstrip("/")
else:
    SUPABASE_URL = _raw_supabase
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
ORATS_KEY      = os.environ.get("ORATS_API_KEY", "")
SNAPSHOT_TABLE = os.environ.get("SNAPSHOT_TABLE", "gex_snapshot")
# Symbol universe: explicit override via BRIEFING_SYMBOLS, else the Supabase
# watchlist filtered by category. Resolution happens in main() after secrets
# are validated (watchlist pull needs SUPABASE_URL + key).
_BRIEFING_SYMBOLS_ENV = os.environ.get("BRIEFING_SYMBOLS", "").strip()
WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "watchlist")
WATCHLIST_CATEGORIES = [c.strip().lower() for c in os.environ.get(
    "WATCHLIST_CATEGORIES", "core,satellite,watch1"
).split(",") if c.strip()]
ORATS_BASE     = os.environ.get("ORATS_BASE", "https://api.orats.io/datav2")
DTE_FILTER     = os.environ.get("ORATS_DTE_FILTER", "0,30")
BATCH_SIZE     = int(os.environ.get("BATCH_SIZE", "1"))
TIMEOUT        = int(os.environ.get("REQUEST_TIMEOUT_SEC", "30"))
RUN_LABEL      = os.environ.get("RUN_LABEL", "manual")
MIN_OI         = int(os.environ.get("GEX_MIN_OI", "50"))
MONEYNESS_PCT  = float(os.environ.get("GEX_MONEYNESS_PCT", "20"))
RESOLUTION_MM  = float(os.environ.get("GEX_RESOLUTION_MM", "1.0"))


def log(level, msg):
    sys.stderr.write(f"[{level}] {msg}\n")
    sys.stderr.flush()


def die(msg, code=1):
    log("ERROR", msg)
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)


# ----------------------------- ORATS ---------------------------------------

def orats_get(path, params):
    """GET ORATS with the API key. Returns parsed JSON or raises."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{ORATS_BASE}/{path}?{qs}"
    if ORATS_KEY and "token" not in params:
        # ORATS commonly uses ?token=<key> on the datav2 surface
        sep = "&" if qs else ""
        url = f"{url}{sep}token={urllib.parse.quote(ORATS_KEY)}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "MavisGexWorkflow/1.0")
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = float(e.headers.get("Retry-After", "1.0"))
                log("WARN", f"ORATS 429, sleeping {wait}s (attempt {attempt}/3)")
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"ORATS HTTP {e.code}: {body}")
        except Exception as e:
            if attempt < 3:
                time.sleep(0.5 * attempt)
                continue
            raise RuntimeError(f"ORATS request failed: {e}")


def fetch_orats(symbol):
    """Fetch delayed strikes + spot for one symbol.

    Returns a dict with keys: spot, rows (list), raw_count, trade_date (YYYY-MM-DD or None).
    Raises RuntimeError on any non-recoverable failure.
    """
    root = symbol.lstrip("^")
    dte_min, dte_max = (int(x) for x in DTE_FILTER.split(","))
    # ORATS datav2: /strikes returns per-strike rows. The token is passed as a query
    # parameter. The endpoint returns {data: [...]} with each row having gamma, callOI, putOI.
    data = orats_get("strikes", {
        "ticker": root,
        "dteMin": dte_min,
        "dteMax": dte_max,
    })
    if not isinstance(data, (list, dict)):
        raise RuntimeError(f"Unexpected ORATS response shape: {type(data).__name__}")
    if isinstance(data, dict):
        rows = data.get("data") or data.get("results") or data.get("rows") or []
    else:
        rows = data
    if not rows:
        raise RuntimeError(f"ORATS returned no rows for {symbol}")

    # Filter to the requested DTE window defensively (in case the endpoint ignored dteMin/dteMax)
    rows = [r for r in rows if dte_min <= (r.get("dte") or 0) <= dte_max]
    if not rows:
        raise RuntimeError(f"ORATS returned no rows in DTE window [{dte_min},{dte_max}] for {symbol}")

    # Spot: prefer stockPrice (per-row), then spotPrice, then median strike
    spot = None
    for r in rows:
        for k in ("stockPrice", "spotPrice"):
            v = r.get(k)
            if v is not None:
                spot = float(v); break
        if spot is not None: break
    if spot is None:
        strikes = [r.get("strike") for r in rows if r.get("strike") is not None]
        if not strikes:
            raise RuntimeError(f"ORATS rows have no strike and no spot for {symbol}")
        spot = float(sum(strikes) / len(strikes))

    # tradeDate from ORATS (the underlying session date, not the cron fire time).
    # ORATS exposes tradeDate at the row level.
    trade_date = None
    for r in rows:
        td = r.get("tradeDate")
        if td:
            trade_date = str(td)[:10]  # YYYY-MM-DD prefix
            break

    return {"spot": spot, "rows": rows, "raw_count": len(rows), "trade_date": trade_date}


# ----------------------------- GEX math ------------------------------------

def normalize_row(r):
    """Map an ORATS row to (strike, type, oi, gamma, dte). Tolerate field aliases."""
    strike = r.get("strike") or r.get("k")
    typ    = (r.get("type") or r.get("cp") or "").lower()
    if typ in ("c", "call"): typ = "call"
    elif typ in ("p", "put"): typ = "put"
    oi     = r.get("oi") or r.get("openInterest") or 0
    gamma  = r.get("gamma") or 0
    dte    = r.get("dte") or 0
    try:
        return float(strike), typ, float(oi), float(gamma), int(dte)
    except (TypeError, ValueError):
        return None


def aggregate(rows, spot, trade_date=None):
    """Compute GEX summary + gamma ladder for one symbol.

    ORATS returns one row per (strike, expiry). Each row has:
      - gamma (per-share, same value for the call and put at that strike)
      - callOpenInterest, putOpenInterest (per-side open interest in contracts)
      - dte (days to expiration)
      - strike

    IMPORTANT — what this function does NOT do:
      It does NOT determine dealer long/short positioning. OI is signed-neutral:
      the same `gamma × OI` contribution comes from a strike whether the dealer
      is long (long gamma) or short (short gamma) the options. The output
      magnitudes are unsigned; the briefing must not label them as
      "dealer long/short gamma" without a separate signed source.

    What it does do:
      1. Filters strikes by min-OI and moneyness (configurable).
      2. Splits each row into call-side and put-side contributions per strike.
      3. Identifies the dominant call wall as the strike >= spot with the
         largest call-side magnitude; put wall as strike <= spot with the
         largest put-side magnitude. If no strike on the correct side meets
         the filter, the wall is null and `walls_undefined` is set in validity.
      4. Computes net GEX (call-side magnitude minus put-side magnitude) in $MM.
         Below the resolution threshold → `regime="sub_resolution"`.
    """
    by_strike = {}  # strike -> {coi, poi, gamma, dte_min}
    moneyness_lo = (1.0 - MONEYNESS_PCT / 100.0) * spot
    moneyness_hi = (1.0 + MONEYNESS_PCT / 100.0) * spot
    for r in rows:
        strike = r.get("strike")
        gamma  = r.get("gamma")
        coi    = r.get("callOpenInterest") or 0
        poi    = r.get("putOpenInterest")  or 0
        dte    = r.get("dte") or 0
        if strike is None or gamma is None:
            continue
        try:
            strike = float(strike)
            gamma  = float(gamma)
            coi    = float(coi)
            poi    = float(poi)
            dte    = int(dte)
        except (TypeError, ValueError):
            continue
        # Moneyness filter
        if not (moneyness_lo <= strike <= moneyness_hi):
            continue
        # OI filter: a strike must have meaningful OI on at least one side.
        if max(coi, poi) < MIN_OI:
            continue
        # Per-strike aggregation: KEEP only the row with the smallest DTE for
        # each strike. ORATS delayed strikes returns one row per (strike, expiry)
        # — summing gamma/OI across expiries would double-count the same
        # underlying exposure and produce GEX values that are ~30x too large.
        # The nearest-DTE row is the cleanest representative for "what's the
        # GEX at this strike right now".
        if strike in by_strike:
            if dte < by_strike[strike]["dte_min"]:
                by_strike[strike] = {"coi": coi, "poi": poi, "gamma": gamma, "dte_min": dte}
            else:
                continue
        else:
            by_strike[strike] = {"coi": coi, "poi": poi, "gamma": gamma, "dte_min": dte}

    CONTRACT_MULT = 100
    per_strike_call = {}
    per_strike_put  = {}
    for strike, a in by_strike.items():
        coef = a["gamma"] * CONTRACT_MULT * spot * 0.01 / 1e6
        per_strike_call[strike] = coef * a["coi"]
        per_strike_put[strike]  = -coef * a["poi"]

    # Call wall: largest call-side magnitude, MUST be at or above spot
    call_wall = None; call_wall_gx = 0.0
    for s, v in per_strike_call.items():
        if s < spot: continue
        if v > call_wall_gx:
            call_wall = s; call_wall_gx = v
    # Put wall: largest put-side magnitude (most negative), MUST be at or below spot
    put_wall = None; put_wall_gx = 0.0
    for s, v in per_strike_put.items():
        if s > spot: continue
        if v < put_wall_gx:
            put_wall = s; put_wall_gx = v

    walls_undefined = (call_wall is None) and (put_wall is None)

    net_gex_mm = sum(per_strike_call.values()) + sum(per_strike_put.values())
    abs_gex_mm = sum(abs(v) for v in per_strike_call.values()) + sum(abs(v) for v in per_strike_put.values())

    all_strikes = set(per_strike_call) | set(per_strike_put)
    peak_strike = None; peak_val = 0.0
    for s in all_strikes:
        v = abs(per_strike_call.get(s, 0.0)) + abs(per_strike_put.get(s, 0.0))
        if v > peak_val:
            peak_val = v; peak_strike = s

    pos_above = sum(v for s, v in per_strike_call.items() if s >= spot)
    pos_below_signed = sum(v for s, v in per_strike_call.items() if s < spot) + sum(v for s, v in per_strike_put.items() if s < spot)

    # Regime: sub-resolution if |net| < threshold
    if abs(net_gex_mm) < RESOLUTION_MM:
        regime = "sub_resolution"
    else:
        regime = "positive" if net_gex_mm > 0 else "negative"

    # Gravity: prefer the dominant wall on the correct side of spot
    if call_wall is not None and (put_wall is None or call_wall_gx >= abs(put_wall_gx)):
        gravity = "up"
    elif put_wall is not None:
        gravity = "down"
    else:
        gravity = "n/a"

    # Ladder
    above = []
    below = []
    for s in all_strikes:
        v = per_strike_call.get(s, 0.0) + per_strike_put.get(s, 0.0)
        entry = {"gex_mm": v, "strike": s, "distance_pct": (s - spot) / spot * 100}
        if s >= spot: above.append(entry)
        else:         below.append(entry)
    above.sort(key=lambda x: -abs(x["gex_mm"]))
    below.sort(key=lambda x: -abs(x["gex_mm"]))
    ladder = (
        [{"side": "above", **e} for e in above[:8]] +
        [{"side": "below", **e} for e in below[:7]]
    )

    # Validity: roll up all reasons
    validity = ["ok"]
    if walls_undefined: validity.append("walls_undefined")
    if abs(net_gex_mm) < RESOLUTION_MM: validity.append("sub_resolution")
    if trade_date:
        try:
            td = dt.datetime.strptime(trade_date, "%Y-%m-%d").date()
            if td.weekday() >= 5:
                validity.append("weekend_tradedate")
        except Exception:
            pass

    return {
        "spot_price": spot,
        "net_gex_mm": round(net_gex_mm, 4),
        "abs_gex_mm": round(abs_gex_mm, 4),
        "zero_gamma": None,  # removed — requires signed source to be meaningful
        "peak_gex_strike": peak_strike,
        "call_wall": call_wall,
        "call_wall_gex_mm": round(call_wall_gx, 4) if call_wall is not None else None,
        "put_wall": put_wall,
        "put_wall_gex_mm": round(put_wall_gx, 4) if put_wall is not None else None,
        "regime": regime,
        "pos_gex_above_spot_mm": round(pos_above, 4),
        "pos_gex_below_spot_mm": round(pos_below_signed, 4),
        "gravity": gravity,
        "gamma_ladder": [
            {"side": l["side"], "gex_mm": round(l["gex_mm"], 6),
             "strike": l["strike"], "distance_pct": round(l["distance_pct"], 3)}
            for l in ladder
        ],
        "strike_count": len(all_strikes),
        "raw_rows": len(rows),
        "validity": validity,
        "signed_source": "none",  # explicit: no signed dealer positioning source wired in
    }


# ----------------------------- Supabase ------------------------------------

def supabase_insert(row):
    """POST a single row to gex_snapshot. The table uses an auto-assigned `id` PK;
    downstream consumers dedupe by snapshot_ts desc. No on_conflict needed."""
    url = f"{SUPABASE_URL}/rest/v1/{SNAPSHOT_TABLE}"
    body = json.dumps([row], default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Supabase HTTP {e.code}: {body}")


def supabase_get(path_qs):
    """GET from the Supabase REST surface. Returns parsed JSON or raises."""
    url = f"{SUPABASE_URL}/rest/v1/{path_qs}"
    req = urllib.request.Request(url)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Supabase HTTP {e.code}: {body}")


# ----------------------------- Symbol universe -----------------------------

def resolve_symbols():
    """Return the ordered, de-duplicated symbol list for this run.

    1. If BRIEFING_SYMBOLS env is set, it wins (explicit override).
    2. Otherwise pull distinct symbols from the watchlist table where
       category is in WATCHLIST_CATEGORIES (default core, satellite, watch1).
       Ordering: WATCHLIST_CATEGORIES order, then symbol.
    """
    if _BRIEFING_SYMBOLS_ENV:
        return [s.strip().upper() for s in _BRIEFING_SYMBOLS_ENV.split(",") if s.strip()]

    cats = ",".join(WATCHLIST_CATEGORIES)
    qs = (f"{urllib.parse.quote(WATCHLIST_TABLE)}?select=symbol,category"
          f"&category=in.({cats})&order=category,symbol")
    rows = supabase_get(qs)
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected watchlist payload: {type(rows).__name__}")
    rank = {c: i for i, c in enumerate(WATCHLIST_CATEGORIES)}
    rows.sort(key=lambda r: (rank.get(str(r.get("category") or "").lower(), 99),
                             str(r.get("symbol") or "")))
    seen, symbols = set(), []
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    if not symbols:
        raise RuntimeError(
            f"Watchlist returned no symbols for categories [{cats}]")
    return symbols


# ----------------------------- Main ----------------------------------------

def process_symbol(symbol):
    """Run the full pipeline for one symbol. Returns a row dict or raises.

    Note: the gex_snapshot table in this Supabase project does not have
    `trade_date`, `validity`, or `signed_source` columns (the existing schema
    was created by an external writer and the REST surface doesn't expose DDL).
    We compute those fields locally and surface them via the INFO log; the
    briefing generation re-derives them from the stored row.
    """
    fetched = fetch_orats(symbol)
    agg = aggregate(fetched["rows"], fetched["spot"], trade_date=fetched.get("trade_date"))
    row = {
        "symbol": symbol,
        "snapshot_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "spot_price": agg["spot_price"],
        "net_gex": round(agg["net_gex_mm"] * 1e6, 2),
        "net_gex_mm": agg["net_gex_mm"],
        "abs_gex_mm": agg["abs_gex_mm"],
        "zero_gamma": agg["zero_gamma"],
        "peak_gex_strike": agg["peak_gex_strike"],
        "call_wall": agg["call_wall"],
        "call_wall_gex_mm": agg["call_wall_gex_mm"],
        "put_wall": agg["put_wall"],
        "put_wall_gex_mm": agg["put_wall_gex_mm"],
        "regime": agg["regime"],
        "near_term_gex_pct": 100.0,
        "pos_gex_above_spot_mm": agg["pos_gex_above_spot_mm"],
        "pos_gex_below_spot_mm": agg["pos_gex_below_spot_mm"],
        "gravity": agg["gravity"],
        "gamma_ladder": agg["gamma_ladder"],
        "strike_count": agg["strike_count"],
        "raw_rows": agg["raw_rows"],
    }
    supabase_insert(row)
    # Attach the local-only fields to the returned dict for the caller
    row["_local"] = {
        "trade_date": fetched.get("trade_date"),
        "validity": agg["validity"],
        "signed_source": agg["signed_source"],
    }
    return row


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        die("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    if not ORATS_KEY:
        die("ORATS_API_KEY not set")

    try:
        SYMBOLS = resolve_symbols()
    except Exception as e:
        die(f"Symbol universe resolution failed: {e}")

    source = "env BRIEFING_SYMBOLS" if _BRIEFING_SYMBOLS_ENV else (
        f"watchlist {WATCHLIST_TABLE} categories={WATCHLIST_CATEGORIES}")
    log("INFO", f"run_label={RUN_LABEL} symbols={len(SYMBOLS)} source={source} "
        f"dte={DTE_FILTER} "
        f"min_oi={MIN_OI} moneyness_pct={MONEYNESS_PCT} resolution_mm={RESOLUTION_MM}")

    processed = []
    failed = []
    for i, sym in enumerate(SYMBOLS, 1):
        try:
            row = process_symbol(sym)
            processed.append(sym)
            local = row.get("_local", {})
            log("INFO", f"[{i}/{len(SYMBOLS)}] {sym} spot={row['spot_price']:.2f} "
                f"regime={row['regime']} cw={row['call_wall']} pw={row['put_wall']} "
                f"gx={row['net_gex_mm']:+.2f}M strikes={row['strike_count']} "
                f"trade_date={local.get('trade_date')} validity={local.get('validity')}")
        except Exception as e:
            failed.append({"symbol": sym, "error": str(e)[:200]})
            log("ERROR", f"[{i}/{len(SYMBOLS)}] {sym} FAILED: {e}")
        # Gentle pacing
        if BATCH_SIZE and i % max(1, BATCH_SIZE) == 0:
            time.sleep(0.5)

    result = {
        "ok": True,
        "processed": len(processed),
        "failed_count": len(failed),
        "failed": failed,
        "run_label": RUN_LABEL,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
