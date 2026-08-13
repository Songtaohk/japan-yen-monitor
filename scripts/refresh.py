#!/usr/bin/env python3
"""
refresh.py — 从官方/公开来源抓取最新市场与宏观数据，写入 data/latest.json
Fetch the live series the report depends on and write data/latest.json.

设计原则 / Design principles
---------------------------
1. 每个来源独立 try/except：任何一个失败都不影响其他来源，也不会让整个 run 失败。
   Each source is independently guarded; one failure never aborts the run.
2. 抓取失败时**保留上一次的值**并标记 stale，绝不写入猜测值。
   On failure the previous value is retained and flagged stale. Never guess.
3. 每个字段都带 source / as_of / status，前端据此显示可信度。
   Every field carries source, as_of and status so the page can show provenance.
4. 只抓取「会变」的市场数据。年度统计（IIP、国际收支、资金循环）变化频率是年/季度，
   由 scripts/refresh.py 的 --annual 模式单独处理，避免每日无谓请求。
"""
from __future__ import annotations
import csv, io, json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "latest.json")
UA = "japan-yen-monitor/1.0 (+https://github.com/)"
TIMEOUT = 45

JST = timezone(timedelta(hours=9))


def get(url: str, encoding: str = "utf-8") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    for enc in (encoding, "utf-8", "cp932", "shift_jis", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------- FRED
FRED_SERIES = {
    "usdjpy":     ("DEXJPUS",  "USD/JPY spot"),
    "ust10":      ("DGS10",    "US 10y Treasury CMT, %"),
    "ust2":       ("DGS2",     "US 2y Treasury CMT, %"),
    "ust30":      ("DGS30",    "US 30y Treasury CMT, %"),
    "fedfunds":   ("DFF",      "Fed funds effective, %"),
    "reer":       ("RBJPBIS",  "Japan BIS real broad EER, 2020=100"),
    "boj_rate":   ("IRSTCI01JPM156N", "Japan immediate rate / call rate, %"),
    "jp_cpi_yoy": ("JPNCPIALLMINMEI", "Japan CPI index (all items)"),
}


def fetch_fred(series_id: str):
    """Return (value, date) of the last non-missing observation."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    txt = get(url)
    rows = list(csv.reader(io.StringIO(txt)))
    if len(rows) < 2:
        raise ValueError("empty FRED csv")
    last = None
    for r in rows[1:]:
        if len(r) < 2:
            continue
        d, v = r[0].strip(), r[1].strip()
        if v in (".", "", "NA"):
            continue
        try:
            last = (float(v), d)
        except ValueError:
            continue
    if last is None:
        raise ValueError("no valid FRED observations")
    return last


# ---------------------------------------------------------------- MOF JGB
# 財務省「国債金利情報」 daily CSV, Shift-JIS. Header: 基準日,1年,2年,...,40年
MOF_JGB = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_JGB_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/historical/jgbcme_all.csv"


def fetch_jgb():
    txt = get(MOF_JGB, encoding="cp932")
    rows = [r for r in csv.reader(io.StringIO(txt)) if r and r[0].strip()]
    hdr_i = None
    for i, r in enumerate(rows):
        joined = "".join(r)
        if "1年" in joined and "10年" in joined:
            hdr_i = i
            break
    if hdr_i is None:
        raise ValueError("JGB header row not found")
    hdr = [c.strip() for c in rows[hdr_i]]
    idx = {}
    for tenor in ("2年", "5年", "10年", "20年", "30年", "40年"):
        if tenor in hdr:
            idx[tenor] = hdr.index(tenor)
    last = None
    for r in rows[hdr_i + 1:]:
        if not r or not r[0].strip():
            continue
        vals = {}
        ok = False
        for tenor, j in idx.items():
            if j < len(r):
                v = r[j].strip()
                if v not in ("", "-", "*"):
                    try:
                        vals[tenor] = float(v)
                        ok = True
                    except ValueError:
                        pass
        if ok:
            last = (r[0].strip(), vals)
    if last is None:
        raise ValueError("no valid JGB rows")
    date, vals = last
    return {
        "jgb2":  vals.get("2年"),
        "jgb10": vals.get("10年"),
        "jgb30": vals.get("30年"),
        "jgb40": vals.get("40年"),
    }, date


# ---------------------------------------------------------------- MOF BOP
# 国際収支総括表：暦年・半期 CSV。列顺序经核实，但表头在部分环境下会乱码，
# 因此这里只取「最后一行的经常收支」并交由前端标注为参考值。
MOF_BOP_CY = ("https://www.mof.go.jp/policy/international_policy/reference/"
              "balance_of_payments/bp_trend/bpnet/sbp/s-1/6s-1-1.csv")
MOF_BOP_FY = ("https://www.mof.go.jp/policy/international_policy/reference/"
              "balance_of_payments/bp_trend/bpnet/sbp/s-1/6s-1-2.csv")


def fetch_bop(url):
    txt = get(url, encoding="cp932")
    rows = [r for r in csv.reader(io.StringIO(txt)) if r and any(c.strip() for c in r)]
    numeric = []
    for r in rows:
        nums = []
        for c in r[1:]:
            c = c.strip().replace(",", "").replace("▲", "-").replace("△", "-")
            try:
                nums.append(float(c))
            except ValueError:
                nums.append(None)
        if nums and nums[0] is not None:
            numeric.append((r[0].strip(), nums))
    if not numeric:
        raise ValueError("no numeric BOP rows")
    label, nums = numeric[-1]
    return {"period": label, "current_account_oku_yen": nums[0]}


# ---------------------------------------------------------------- driver
def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fields": {}}


def main():
    prev = load_previous()
    prev_fields = prev.get("fields", {})
    fields, errors = {}, []

    def record(key, value, as_of, source, note=""):
        fields[key] = {"value": value, "as_of": as_of, "source": source,
                       "status": "ok", "note": note}

    def keep_stale(key, why):
        old = prev_fields.get(key)
        if old:
            old = dict(old)
            old["status"] = "stale"
            old["error"] = why
            fields[key] = old
        errors.append(f"{key}: {why}")

    # --- FRED ---
    for key, (sid, desc) in FRED_SERIES.items():
        try:
            v, d = fetch_fred(sid)
            record(key, v, d, f"FRED:{sid}", desc)
        except Exception as e:                                   # noqa: BLE001
            keep_stale(key, f"FRED {sid} failed: {e}")

    # --- MOF JGB (authoritative; overrides nothing but adds tenors) ---
    try:
        vals, d = fetch_jgb()
        for k, v in vals.items():
            if v is not None:
                record(k, v, d, "MOF 国債金利情報 jgbcm.csv", "財務省日次公表")
    except Exception as e:                                       # noqa: BLE001
        for k in ("jgb2", "jgb10", "jgb30", "jgb40"):
            keep_stale(k, f"MOF JGB failed: {e}")

    # --- MOF BOP (informational; period label + current account) ---
    for key, url in (("bop_cy", MOF_BOP_CY), ("bop_fy", MOF_BOP_FY)):
        try:
            r = fetch_bop(url)
            record(key, r["current_account_oku_yen"], r["period"],
                   "MOF 国際収支状況", "単位:億円。列順序は目視確認済だが表頭が環境依存で化けるため参考値")
        except Exception as e:                                   # noqa: BLE001
            keep_stale(key, f"MOF BOP failed: {e}")

    # --- derived ---
    def val(k):
        f = fields.get(k)
        return f["value"] if f and isinstance(f.get("value"), (int, float)) else None

    derived = {}
    ust10, jgb10 = val("ust10"), val("jgb10")
    if ust10 is not None and jgb10 is not None:
        derived["spread10_pp"] = round(ust10 - jgb10, 3)
    ff, boj = val("fedfunds"), val("boj_rate")
    if ff is not None and boj is not None:
        derived["spread_policy_pp"] = round(ff - boj, 3)
    ust2, jgb2 = val("ust2"), val("jgb2")
    if ust2 is not None and jgb2 is not None:
        derived["spread2_pp"] = round(ust2 - jgb2, 3)

    # FX-hedged UST vs JGB. Hedge cost approximated by the short-rate gap plus
    # an assumed cross-currency basis. The basis is an ASSUMPTION, not fetched.
    BASIS_ASSUMPTION_PP = 0.20
    if ff is not None and boj is not None and ust10 is not None and jgb10 is not None:
        hedge = ff - boj + BASIS_ASSUMPTION_PP
        derived["hedge_cost_pp"] = round(hedge, 3)
        derived["hedged_ust10_pct"] = round(ust10 - hedge, 3)
        derived["jgb_advantage_bp"] = round((jgb10 - (ust10 - hedge)) * 100, 1)
        derived["_hedge_note"] = (
            f"対冲成本 = 联邦基金 − 日本隔夜 + 假设基差 {BASIS_ASSUMPTION_PP}pp。"
            "基差为假设值，非抓取值 / basis is an assumption, not fetched.")

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "baseline": {
            "note": "报告 v4 撰写时的基准值，用于计算「自发布以来的变化」",
            "as_of": "2026-08-12",
            "usdjpy": 159.40, "jgb10": 2.834, "ust10": 4.72, "ust2": 4.25,
            "ust30": 5.25, "fedfunds": 3.63, "boj_rate": 1.00, "reer": 65.30,
            "spread10_pp": 1.886, "jgb_advantage_bp": 100.0,
        },
        "fields": fields,
        "derived": derived,
        "errors": errors,
        "ok_count": sum(1 for f in fields.values() if f.get("status") == "ok"),
        "stale_count": sum(1 for f in fields.values() if f.get("status") == "stale"),
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"wrote {OUT}: ok={out['ok_count']} stale={out['stale_count']}")
    for e in errors:
        print("  WARN", e, file=sys.stderr)
    # Never fail the workflow on a single source outage — but do fail if
    # literally nothing was fetched, which means the runner has no network.
    return 0 if out["ok_count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
