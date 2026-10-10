"""pull_jlaw.py — 从 Supabase trade-jlaw-v2 拉最新 jlaw 双管线数据到本地 JSON。

产出：
  briefing_yahoo.json  最新一个 yahoo 交易日的全部行
  briefing_chart.json   最新一个 chart 交易日的全部行

用法：
  python scripts/pull_jlaw.py --out <输出目录，默认当前目录>

依赖：requests。环境：工作区 .env 含小写 supabase_url / supabase_service_role。
"""
import argparse
import json
import os
import sys

import requests

TABLE = "trade-jlaw-v2"


def load_env():
    env = {}
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    env.setdefault("supabase_url", os.environ.get("SUPABASE_URL", ""))
    env.setdefault("supabase_service_role", os.environ.get("SUPABASE_SERVICE_ROLE", ""))
    return env


def base_url(raw):
    raw = raw.rstrip("/")
    # base 若已含 /rest/v1 不要重复拼
    return raw if raw.endswith("/rest/v1") else raw + "/rest/v1"


def get_with_retry(url, headers, params, tries=5):
    """Supabase REST 连接偶发 RemoteDisconnected，自动重试。"""
    import time
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=100)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def fetch_all(base, key, review_type):
    """分页拉取某 review_type 最新交易日的全部行。"""
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    # 先找最新 run_date
    r = get_with_retry(
        f"{base}/{TABLE}",
        headers,
        {
            "select": "run_date",
            "jlaw_review_type": f"eq.{review_type}",
            "order": "run_date.desc",
            "limit": 1,
        },
    )
    rows = r.json()
    if not rows:
        return None, []
    latest = rows[0]["run_date"]

    # 分页拉全
    out, offset = [], 0
    while True:
        r = get_with_retry(
            f"{base}/{TABLE}",
            headers,
            {
                "select": "*",
                "jlaw_review_type": f"eq.{review_type}",
                "run_date": f"eq.{latest}",
                "order": "symbol.asc",
                "offset": offset,
                "limit": 500,
            },
        )
        batch = r.json()
        out.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return latest, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    env = load_env()
    if not env["supabase_url"] or not env["supabase_service_role"]:
        sys.exit("缺少 supabase_url / supabase_service_role（.env 或环境变量）")
    base = base_url(env["supabase_url"])

    for rtype, fname in [("yahoo", "briefing_yahoo.json"), ("chart", "briefing_chart.json")]:
        latest, rows = fetch_all(base, env["supabase_service_role"], rtype)
        path = os.path.join(args.out, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"run_date": latest, "count": len(rows), "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"{rtype}: run_date={latest} rows={len(rows)} -> {path}")


if __name__ == "__main__":
    main()
