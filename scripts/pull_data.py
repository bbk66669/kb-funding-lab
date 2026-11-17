#!/usr/bin/env python3
"""
pull_data.py — KB 第一轮核算 · 阶段1 数据拉取（只用 OKX 公开接口，无需密钥）

数据来源（OKX 官方公开 API，https://www.okx.com）：
  - 合约/现货规格:  GET /api/v5/public/instruments
  - 保证金档位:    GET /api/v5/public/position-tiers (逐仓 MMR/IMR)
  - 资金费率历史:  GET /api/v5/public/funding-rate-history
                   按时间戳向后分页（after = 已见最旧 fundingTime），不写死页数，
                   直到覆盖到目标起点或接口返回空。
  - 1H K线:        GET /api/v5/market/history-candles
                   按时间戳向后分页（after = 已见最旧 ts），覆盖整个窗口。
  - 标记价格 1H:   GET /api/v5/market/history-mark-price-candles
                   分页语义 2026-11-17 实测：after = 返回更旧数据；before 返回空。
                   另有 --only-mark --start-ms/--end-ms 补充模式：只补拉冻结窗口的标记价格，
                   不触碰既有数据文件，请求逐条追加记录到 MANIFEST。

产出（data/ 目录）：
  - instruments.json / position_tiers.json（原始返回）
  - funding_<SYMBOL>.json + .csv（去重、按 fundingTime 升序）
  - candles_<INST>.json + .csv（去重、按 ts 升序）
  - MANIFEST.md（每次请求的参数与返回条数、覆盖范围、缺口/重复检查）

只做研究与模拟，不涉及任何账户与订单。
"""
import csv, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

BASE = "https://www.okx.com"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
os.makedirs(DATA, exist_ok=True)

DAYS = 90
SETTLE_MS = 8 * 3600 * 1000          # 资金费结算间隔（BTC/ETH 永续为 8h）
HOUR_MS = 3600 * 1000

REQUEST_LOG = []  # (endpoint, params, n_returned)

def get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kb-funding-lab/0.1"})
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
            if body.get("code") != "0":
                raise RuntimeError(f"OKX code={body.get('code')} msg={body.get('msg')}")
            REQUEST_LOG.append((path, dict(params), len(body.get("data", []))))
            time.sleep(0.15)
            return body["data"]
        except Exception as e:  # 简单重试
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last_err}")

def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ---------- 标记价格 K线（1H）：分页语义经 2026-11-17 实测确认 ----------
# history-mark-price-candles: after=<ts> 返回更旧的数据（与成交 K 线一致）；before 返回空。
# 返回字段 [ts, open, high, low, close, confirm]，ts 为小时开始时间，close 为该小时结束时标记价格。
def pull_mark(inst_id, need_from_ms, need_to_ms):
    """拉取 ts ∈ [need_from_ms, need_to_ms] 的已确认标记价格 1H K线（升序）。"""
    rows, after = {}, None
    while True:
        params = {"instId": inst_id, "bar": "1H", "limit": 100}
        if after is not None:
            params["after"] = after
        page = get("/api/v5/market/history-mark-price-candles", params)
        if not page:
            break
        for c in page:
            ts = int(c[0])
            if need_from_ms <= ts <= need_to_ms and (len(c) <= 5 or c[5] != "0"):
                rows[ts] = c
        oldest = min(int(c[0]) for c in page)
        if oldest <= need_from_ms:
            break
        after = oldest
    return [rows[k] for k in sorted(rows)]

def mark_stats(rows):
    times = [int(c[0]) for c in rows]
    if not times:
        return {"n": 0, "first": None, "last": None, "missing_hours": None}
    expected = (times[-1] - times[0]) // HOUR_MS + 1
    return {"n": len(rows), "first": times[0], "last": times[-1],
            "missing_hours": int(expected - len(rows))}

def write_mark_files(inst_id, rows):
    json.dump(rows, open(os.path.join(DATA, f"mark_{inst_id}.json"), "w"), indent=1, ensure_ascii=False)
    with open(os.path.join(DATA, f"mark_{inst_id}.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts", "tsISO", "open", "high", "low", "close", "confirm"])
        for c in rows:
            w.writerow([c[0], iso(c[0]), c[1], c[2], c[3], c[4], c[5] if len(c) > 5 else ""])

# ---------- 补充模式：只补拉标记价格（冻结样本用，不触碰既有数据文件） ----------
# 用法: python3 scripts/pull_data.py --only-mark --start-ms <T0> --end-ms <T1>
if "--only-mark" in sys.argv:
    args = sys.argv
    w_start = int(args[args.index("--start-ms") + 1])
    w_end = int(args[args.index("--end-ms") + 1])
    log_from = len(REQUEST_LOG)
    stats = {}
    for inst_id in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
        rows = pull_mark(inst_id, w_start - HOUR_MS, w_end - HOUR_MS)
        write_mark_files(inst_id, rows)
        stats[inst_id] = mark_stats(rows)
        print(inst_id, "mark rows:", stats[inst_id])
    with open(os.path.join(DATA, "MANIFEST.md"), "a") as f:
        f.write("\n## 标记价格补充拉取（--only-mark，冻结窗口）\n\n")
        f.write(f"- 补拉时间(UTC): {iso(int(time.time() * 1000))}\n- 窗口: {iso(w_start)} .. {iso(w_end)}"
                f"（需要 ts ∈ [{iso(w_start - HOUR_MS)}, {iso(w_end - HOUR_MS)}] 的标记 K线，close 即结算/事件时刻的标记价格）\n")
        f.write("- 接口: /api/v5/market/history-mark-price-candles（after=向旧分页，实测确认；before 返回空）\n\n")
        f.write("| 合约 | 条数 | 首条ts | 末条ts | 缺失小时数 |\n|---|---|---|---|---|\n")
        for k, s in stats.items():
            f.write(f"| {k} | {s['n']} | {iso(s['first'])} | {iso(s['last'])} | {s['missing_hours']} |\n")
        f.write("\n### 本次补拉请求日志\n\n```\n")
        for path, params, n in REQUEST_LOG[log_from:]:
            f.write(f"{path} {json.dumps(params)} -> {n}\n")
        f.write("```\n")
    print("MANIFEST appended (mark supplement), requests:", len(REQUEST_LOG) - log_from)
    sys.exit(0)

# ---------- 目标窗口：以最新结算时刻为终点，向前 90 天，对齐到 8h 边界 ----------
now_ms = int(time.time() * 1000)
probe = get("/api/v5/public/funding-rate", {"instId": "BTC-USDT-SWAP"})
latest_settle = int(probe[0]["fundingTime"])          # 下一个结算时刻
end_ms = latest_settle - SETTLE_MS                    # 最近一个已结算时刻
start_ms = end_ms - DAYS * 24 * 3600 * 1000           # 目标起点（已对齐 8h）
print(f"window target: {iso(start_ms)} .. {iso(end_ms)}")

# ---------- 1) 规格与档位 ----------
insts = {}
for inst_id, typ in [("BTC-USDT", "SPOT"), ("ETH-USDT", "SPOT"),
                     ("BTC-USDT-SWAP", "SWAP"), ("ETH-USDT-SWAP", "SWAP")]:
    d = get("/api/v5/public/instruments", {"instType": typ, "instId": inst_id})
    insts[inst_id] = d[0]
json.dump(insts, open(os.path.join(DATA, "instruments.json"), "w"), indent=1, ensure_ascii=False)

tiers = {}
for fam in ["BTC-USDT", "ETH-USDT"]:
    tiers[fam] = get("/api/v5/public/position-tiers",
                     {"instType": "SWAP", "tdMode": "isolated", "instFamily": fam})
json.dump(tiers, open(os.path.join(DATA, "position_tiers.json"), "w"), indent=1, ensure_ascii=False)

# ---------- 2) 资金费率历史：按时间戳分页拉到目标起点 ----------
def pull_funding(inst_id):
    rows, after = {}, None
    while True:
        params = {"instId": inst_id, "limit": 100}
        if after is not None:
            params["after"] = after
        page = get("/api/v5/public/funding-rate-history", params)
        if not page:
            break
        for r in page:
            rows[int(r["fundingTime"])] = r      # 以 fundingTime 为键去重
        oldest = min(int(r["fundingTime"]) for r in page)
        if oldest <= start_ms:
            break
        after = oldest
    out = [rows[k] for k in sorted(rows)]
    return out

funding_stats = {}
for sym in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
    rows = pull_funding(sym)
    json.dump(rows, open(os.path.join(DATA, f"funding_{sym}.json"), "w"), indent=1, ensure_ascii=False)
    with open(os.path.join(DATA, f"funding_{sym}.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["fundingTime", "fundingTimeISO", "fundingRate", "realizedRate"])
        for r in rows:
            w.writerow([r["fundingTime"], iso(r["fundingTime"]), r["fundingRate"], r.get("realizedRate", "")])
    times = [int(r["fundingTime"]) for r in rows]
    gaps = [b - a for a, b in zip(times, times[1:])]
    from collections import Counter
    funding_stats[sym] = {
        "n": len(rows), "first": times[0], "last": times[-1],
        "interval_hist": Counter(gaps), "dup_removed_note": "dict-keyed by fundingTime",
    }
    print(sym, "funding rows:", len(rows), iso(times[0]), "->", iso(times[-1]))

# ---------- 3) 1H K线：现货与永续，按时间戳分页覆盖窗口 ----------
def pull_candles(inst_id):
    rows, after = {}, None
    need_from = start_ms - HOUR_MS  # 多拉一根，保证起点有价
    while True:
        params = {"instId": inst_id, "bar": "1H", "limit": 100}
        if after is not None:
            params["after"] = after
        page = get("/api/v5/market/history-candles", params)
        if not page:
            break
        for c in page:
            rows[int(c[0])] = c
        oldest = min(int(c[0]) for c in page)
        if oldest <= need_from:
            break
        after = oldest
    return [rows[k] for k in sorted(rows)]

candle_stats = {}
for inst_id in ["BTC-USDT", "ETH-USDT", "BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
    rows = pull_candles(inst_id)
    json.dump(rows, open(os.path.join(DATA, f"candles_{inst_id}.json"), "w"), indent=1, ensure_ascii=False)
    with open(os.path.join(DATA, f"candles_{inst_id}.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts", "tsISO", "open", "high", "low", "close", "confirm"])
        for c in rows:
            w.writerow([c[0], iso(c[0]), c[1], c[2], c[3], c[4], c[8] if len(c) > 8 else ""])
    times = [int(c[0]) for c in rows]
    expected = (times[-1] - times[0]) // HOUR_MS + 1
    candle_stats[inst_id] = {"n": len(rows), "first": times[0], "last": times[-1],
                             "missing_hours": int(expected - len(rows))}
    print(inst_id, "candles:", len(rows), iso(times[0]), "->", iso(times[-1]), "missing:", expected - len(rows))

# ---------- 3b) 标记价格 1H：覆盖同一窗口（ts ∈ [start-1h, end-1h]） ----------
mark_cov = {}
for inst_id in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
    rows = pull_mark(inst_id, start_ms - HOUR_MS, end_ms - HOUR_MS)
    write_mark_files(inst_id, rows)
    mark_cov[inst_id] = mark_stats(rows)
    print(inst_id, "mark candles:", mark_cov[inst_id])

# ---------- 4) MANIFEST ----------
with open(os.path.join(DATA, "MANIFEST.md"), "w") as f:
    f.write("# 数据清单 MANIFEST（pull_data.py 自动生成）\n\n")
    f.write(f"- 拉取时间(UTC): {iso(now_ms)}\n- 目标窗口: {iso(start_ms)} .. {iso(end_ms)}（最近 {DAYS} 天，对齐 8h 结算边界）\n")
    f.write("- 数据源: OKX 官方公开接口 www.okx.com（无密钥）\n\n")
    f.write("## 资金费率覆盖与完整性\n\n| 合约 | 条数 | 首条 | 末条 | 相邻间隔分布(ms×次数) |\n|---|---|---|---|---|\n")
    for sym, s in funding_stats.items():
        hist = ", ".join(f"{k}×{v}" for k, v in sorted(s["interval_hist"].items()))
        f.write(f"| {sym} | {s['n']} | {iso(s['first'])} | {iso(s['last'])} | {hist} |\n")
    f.write("\n（间隔应全部为 28800000=8h；出现其他值即为缺口或结算周期变更，报告中须注明。）\n\n")
    f.write("## K线覆盖（1H）\n\n| 交易对 | 条数 | 首条 | 末条 | 缺失小时数 |\n|---|---|---|---|---|\n")
    for k, s in candle_stats.items():
        f.write(f"| {k} | {s['n']} | {iso(s['first'])} | {iso(s['last'])} | {s['missing_hours']} |\n")
    f.write("\n## 标记价格 K线覆盖（1H，history-mark-price-candles）\n\n| 合约 | 条数 | 首条ts | 末条ts | 缺失小时数 |\n|---|---|---|---|---|\n")
    for k, s in mark_cov.items():
        f.write(f"| {k} | {s['n']} | {iso(s['first'])} | {iso(s['last'])} | {s['missing_hours']} |\n")
    f.write("\n## 全部请求日志（endpoint, 参数, 返回条数）\n\n```\n")
    for path, params, n in REQUEST_LOG:
        f.write(f"{path} {json.dumps(params)} -> {n}\n")
    f.write("```\n")
print("MANIFEST written, total requests:", len(REQUEST_LOG))
