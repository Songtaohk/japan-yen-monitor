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


# ------------------------------------------------- BOJ policy rate (ADMINISTERED)
# A policy rate is a discrete decision by a committee, NOT a scrapeable series.
# FRED's OECD series IRSTCI01JPM156N is a MONTHLY AVERAGE of the call rate: in a
# month containing a rate change it blends both levels (June 2026 printed 0.84%
# because the 0.75% -> 1.00% hike landed mid-month). Using it as "the policy rate"
# silently corrupts every derived spread. So the policy rate is hard-coded here
# from the BOJ's own decision, and the FRED series is kept only as a cross-check.
#
# ⚠️ UPDATE THIS AFTER EVERY BOJ MEETING THAT CHANGES THE RATE.
#    Source: https://www.boj.or.jp/en/mopo/mpmdeci/index.htm
BOJ_POLICY_RATE = 1.00          # %
BOJ_POLICY_RATE_ASOF = "2026-06-16"
BOJ_POLICY_RATE_SRC = "BOJ Monetary Policy Meeting decision, 16 Jun 2026"
# Drift check, deliberately ASYMMETRIC. The monthly average LAGS the policy rate:
# in the month of a hike it sits BELOW it (June 2026: avg 0.84 vs policy 1.00), so a
# modest negative gap is normal and must not cry wolf. But the average sitting ABOVE
# the recorded policy rate means the market is paying more than we think the policy
# is — i.e. a hike we failed to record. That is the case worth shouting about.
BOJ_DRIFT_WARN_UP_PP = 0.10     # avg above policy  -> probable missed HIKE
BOJ_DRIFT_WARN_DOWN_PP = 0.40   # avg far below     -> probable missed CUT / stale constant


# ---------------------------------------------------------------- FRED
FRED_SERIES = {
    "usdjpy":   ("DEXJPUS", "USD/JPY spot"),
    "ust10":    ("DGS10",   "US 10y Treasury CMT, %"),
    "ust2":     ("DGS2",    "US 2y Treasury CMT, %"),
    "ust30":    ("DGS30",   "US 30y Treasury CMT, %"),
    "fedfunds": ("DFF",     "Fed funds effective, %"),
    "reer":     ("RBJPBIS", "Japan BIS real broad EER, 2020=100"),
}
# Fetched but NOT used as the policy rate — cross-check only.
FRED_CROSSCHECK = {
    "jp_call_rate_monthly_avg": ("IRSTCI01JPM156N",
                                 "Japan call money, OECD MONTHLY AVERAGE (not the policy rate)"),
}
# Index level -> we derive year-on-year ourselves.
FRED_YOY = {
    "jp_cpi_yoy": ("JPNCPIALLMINMEI", "Japan CPI all items, y/y % derived from index"),
    "us_cpi_yoy": ("CPIAUCSL", "US CPI all items SA, y/y % derived from index"),
}


def fetch_fred_series(series_id: str):
    """Return [(date_str, float), ...] of all non-missing observations."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    rows = list(csv.reader(io.StringIO(get(url))))
    if len(rows) < 2:
        raise ValueError("empty FRED csv")
    out = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        d, v = r[0].strip(), r[1].strip()
        if v in (".", "", "NA"):
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    if not out:
        raise ValueError("no valid FRED observations")
    return out


def fetch_fred(series_id: str):
    """Last observation as (value, date)."""
    obs = fetch_fred_series(series_id)
    d, v = obs[-1]
    return v, d


def fetch_fred_yoy(series_id: str):
    """Year-on-year % change of an index series, as (pct, date).

    Monthly series: compare against the observation 12 prints back, but only
    after confirming the dates really are ~12 months apart, so a gap in the
    series cannot silently produce a nonsense growth rate.
    """
    obs = fetch_fred_series(series_id)
    if len(obs) < 13:
        raise ValueError("series too short for y/y")

    def ym(ds):
        return int(ds[:4]) * 12 + int(ds[5:7])

    d_now, v_now = obs[-1]
    target = ym(d_now) - 12
    # Scan backwards for the observation whose month is exactly 12 before the
    # latest. Position -13 is wrong whenever the series has a missing month —
    # which is how the first US CPI attempt produced a 13-month comparison.
    cand = None
    for ds, v in reversed(obs[:-1]):
        m = ym(ds)
        if m == target:
            cand = (ds, v)
            break
        if m < target:
            break
    if cand is None:
        raise ValueError(
            f"no observation exactly 12 months before {d_now}; series has a gap there")
    d_prev, v_prev = cand
    if v_prev == 0:
        raise ValueError("zero base")
    return round((v_now / v_prev - 1) * 100, 2), d_now


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


def _clean_num(c: str):
    """Parse a Japanese-statistics numeric cell. Returns float or None."""
    if c is None:
        return None
    c = c.strip().strip('"').strip("'")
    # full-width space, thousands separators, and every minus-sign variant used by MOF
    for a, b in (("\u3000", ""), (",", ""), ("\uFF0C", ""),
                 ("\u25B2", "-"), ("\u25B3", "-"),   # ▲ △
                 ("\u2212", "-"), ("\u2013", "-"), ("\u2014", "-"),  # − – —
                 ("\uFF0D", "-"), ("\uFF0B", "+"),  # full-width -, +
                 (" ", "")):
        c = c.replace(a, b)
    if c in ("", "-", "*", "...", "\u2026", "n.a.", "NA", "--"):
        return None
    try:
        return float(c)
    except ValueError:
        return None


def fetch_bop(url):
    """Scan a MOF BOP CSV and return the last row that looks like data.

    The header row is unreliable (encoding varies by environment), so instead of
    trusting column names we look for the last row containing at least MIN_NUM
    parseable numbers and take its first numeric cell as the current account.
    That column position is verified against MOF's published table layout.
    Raises with a diagnostic excerpt when nothing matches, so a future failure
    shows what the endpoint actually returned instead of just "no rows".
    """
    MIN_NUM = 3
    txt = get(url, encoding="cp932")

    head = txt[:300].lstrip().lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        raise ValueError(f"endpoint returned HTML, not CSV: {txt[:120]!r}")

    # skipinitialspace: MOF sometimes emits `, "174,292"` — without this the
    # quote is not honoured and the thousands separator becomes a delimiter.
    rows = list(csv.reader(io.StringIO(txt), skipinitialspace=True))
    best = None
    for r in rows:
        if not r:
            continue
        nums = [(i, _clean_num(c)) for i, c in enumerate(r)]
        nums = [(i, v) for i, v in nums if v is not None]
        if len(nums) >= MIN_NUM:
            # Drop a leading bare year cell (e.g. "2025") so the first numeric
            # is the current account, not the period.
            if len(nums) > MIN_NUM and 1900 <= nums[0][1] <= 2100 and nums[0][1].is_integer():
                nums = nums[1:]
            label = ""
            for c in r:
                c = c.strip()
                if c and _clean_num(c) is None:
                    label = c
                    break
            best = {"period": label or "(unlabelled)",
                    "current_account_oku_yen": nums[0][1],
                    "n_numeric": len(nums)}
    if best is None:
        sample = " | ".join(",".join(r)[:80] for r in rows[:6]) or "(empty response)"
        raise ValueError(
            f"no data row with >= {MIN_NUM} numbers; {len(rows)} rows seen; first rows: {sample}")
    return best


# ---------------------------------------------------------------- driver
# Every field this script is supposed to produce. Checked at the end of main():
# if a whole source silently stops being called (e.g. an editing accident removes
# its driver block), the run reports it loudly instead of just returning a smaller
# ok_count that nobody notices. This exact regression happened once — the MOF JGB
# and BOP blocks were deleted by a bad patch and the run still exited 0.
EXPECTED_FIELDS = {
    "usdjpy", "ust10", "ust2", "ust30", "fedfunds", "reer",
    "boj_rate", "jp_call_rate_monthly_avg", "jp_cpi_yoy", "us_cpi_yoy",
    "jgb2", "jgb10", "jgb30", "jgb40", "bop_cy", "bop_fy",
}
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

    # --- BOJ policy rate: administered, not scraped ---
    record("boj_rate", BOJ_POLICY_RATE, BOJ_POLICY_RATE_ASOF, BOJ_POLICY_RATE_SRC,
           "手动维护：政策利率是央行决议的离散值，不是可抓取序列。每次议息后需更新脚本常量。")

    # --- FRED market series ---
    for key, (sid, desc) in FRED_SERIES.items():
        try:
            v, d = fetch_fred(sid)
            record(key, v, d, f"FRED:{sid}", desc)
        except Exception as e:                                   # noqa: BLE001
            keep_stale(key, f"FRED {sid} failed: {e}")

    # --- FRED cross-check series (never used in derivations) ---
    for key, (sid, desc) in FRED_CROSSCHECK.items():
        try:
            v, d = fetch_fred(sid)
            record(key, v, d, f"FRED:{sid}", desc)
        except Exception as e:                                   # noqa: BLE001
            keep_stale(key, f"FRED {sid} failed: {e}")

    # --- year-on-year rates derived from index levels ---
    for key, (sid, desc) in FRED_YOY.items():
        try:
            v, d = fetch_fred_yoy(sid)
            record(key, v, d, f"FRED:{sid} (y/y derived)", desc)
        except Exception as e:                                   # noqa: BLE001
            keep_stale(key, f"FRED {sid} y/y failed: {e}")

    # --- drift check: has the BOJ moved without the constant being updated? ---
    cc = fields.get("jp_call_rate_monthly_avg")
    if cc and isinstance(cc.get("value"), (int, float)):
        drift = cc["value"] - BOJ_POLICY_RATE
        why = None
        if drift > BOJ_DRIFT_WARN_UP_PP:
            why = "call rate is ABOVE the recorded policy rate — a HIKE was probably missed"
        elif drift < -BOJ_DRIFT_WARN_DOWN_PP:
            why = "call rate is far BELOW the recorded policy rate — a CUT was probably missed, or the constant is stale"
        if why:
            errors.append(
                f"BOJ_POLICY_RATE={BOJ_POLICY_RATE}% (as of {BOJ_POLICY_RATE_ASOF}) vs call-rate "
                f"monthly average {cc['value']}% ({cc['as_of']}), drift {drift:+.2f}pp: {why}. "
                f"UPDATE BOJ_POLICY_RATE in scripts/refresh.py.")
            fields["boj_rate"]["note"] += f"  ⚠️ 与月均值偏离 {drift:+.2f}pp，请核对政策利率。"
        else:
            fields["boj_rate"]["note"] += (
                f"  交叉检查：月均值 {cc['value']}%（{cc['as_of']}），偏离 {drift:+.2f}pp，"
                "在加息当月的正常混合范围内。")

    # --- MOF JGB (authoritative daily source for Japanese government bond yields) ---
    try:
        vals, d = fetch_jgb()
        for k, v in vals.items():
            if v is not None:
                record(k, v, d, "MOF 国債金利情報 jgbcm.csv", "財務省日次公表")
    except Exception as e:                                       # noqa: BLE001
        for k in ("jgb2", "jgb10", "jgb30", "jgb40"):
            keep_stale(k, f"MOF JGB failed: {e}")

    # --- MOF BOP (informational: period label + current account) ---
    for key, url in (("bop_cy", MOF_BOP_CY), ("bop_fy", MOF_BOP_FY)):
        try:
            r = fetch_bop(url)
            record(key, r["current_account_oku_yen"], r["period"], "MOF 国際収支状況",
                   "単位:億円。列位置解析のため参考値 / column-position parse, treat as indicative")
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

    never_attempted = EXPECTED_FIELDS - set(fields)
    if never_attempted:
        errors.append(
            "FIELDS NEVER ATTEMPTED (a source's driver block may have been removed): "
            + ", ".join(sorted(never_attempted)))

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
        "expected_count": len(EXPECTED_FIELDS),
        "never_attempted": sorted(never_attempted),
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"wrote {OUT}: ok={out['ok_count']} stale={out['stale_count']} "
          f"expected={out['expected_count']}")
    if never_attempted:
        print("  !! NEVER ATTEMPTED:", ", ".join(sorted(never_attempted)), file=sys.stderr)
    for e in errors:
        print("  WARN", e, file=sys.stderr)
    # Never fail the workflow on a single source outage — but do fail if
    # literally nothing was fetched, which means the runner has no network.
    return 0 if out["ok_count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
