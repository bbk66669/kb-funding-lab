#!/usr/bin/env python3
"""
independent_tests.py — KB 第一轮阶段1 v3 独立测试（与 benchmark.py 内部自查分开）

运行: python3 scripts/independent_tests.py
产出: results/independent_tests.txt（PASS/FAIL + 关键数值），失败时退出码非 0。

四项针对性测试（各自能抓一类确定错误）：
  T-A 现金流水独立核对：本文件内另一份实现从原始数据逐项重算完整生命周期现金流，
      与主表 CSV 对照（抓退出滑点重复扣等记账错误，不查程序内部恒等式）。
  T-B 未来价格扰动不变性：冻结数据副本中只改 T1 现货/永续开盘价 (×1.5) 后重跑程序，
      开仓配置（张数/数量/保证金/现金池/费用预留）必须逐项不变（抓开仓配置前视）。
  T-C 成本基准检查：(a) 用开仓成交价 P0 重算触发时权益须与 CSV 一致、用开仓标记价
      MK0 重算须系统性偏离；(b) 只扰动 MK0（T0 标记收盘 ×1.05）重跑，主表须逐字节
      不变（抓标记价替代成本价）。
  T-D 清算阈值公式检查：按 OKX 强平 FAQ 公式逐小时手算清算价，程序触发时刻须为
      首个标记收盘 ≥ 清算价的事件、且前一事件不触发（抓阈值漏费用项）。
"""
import csv, json, math, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DATA = os.path.join(ROOT, "data")
RES = os.path.join(ROOT, "results")
SPOT_TAKER, PERP_TAKER, MARGIN_FRAC = 0.0010, 0.0005, 0.35
HOUR = 3600 * 1000
TOL = 0.01

results = []
def record(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ---------- 独立迷你模型（按规格从原始数据重算，不 import benchmark） ----------
def mini(base, C, mode, bps, data_dir=DATA):
    def load(n):
        return json.load(open(os.path.join(data_dir, n)))
    insts = load("instruments.json"); tiers = load("position_tiers.json")
    fund = {}
    for b in ["BTC", "ETH"]:
        fund[b] = {int(r["fundingTime"]): r for r in load(f"funding_{b}-USDT-SWAP.json")}
    common = sorted(set(fund["BTC"]) & set(fund["ETH"]))
    T1 = common[-1]; T0 = min(t for t in common if t >= T1 - 90 * 24 * 3600 * 1000)
    settles = [t for t in common if T0 < t <= T1]
    events = [T0 + i * HOUR for i in range(1, int((T1 - T0) // HOUR) + 1)]
    mk = {int(c[0]): float(c[4]) for c in load(f"mark_{base}-USDT-SWAP.json")}
    so = {int(c[0]): float(c[1]) for c in load(f"candles_{base}-USDT.json")}
    po = {int(c[0]): float(c[1]) for c in load(f"candles_{base}-USDT-SWAP.json")}
    sc = {int(c[0]): float(c[4]) for c in load(f"candles_{base}-USDT.json")}
    S0, P0, S1, P1 = so[T0], po[T0], so[T1], po[T1]
    MK0 = mk[T0 - HOUR]
    ct = float(insts[f"{base}-USDT-SWAP"]["ctVal"]); lot = float(insts[f"{base}-USDT-SWAP"]["lotSz"])
    mmr = float(tiers[f"{base}-USDT"][0]["mmr"])
    qty_t = C / (S0 * (1 + 2 * SPOT_TAKER) + P0 * (MARGIN_FRAC + 2 * PERP_TAKER))
    contracts = math.floor((qty_t / ct) / lot) * lot
    qty = contracts * ct; net_qty = qty * (1 - SPOT_TAKER)
    sn0, pn0 = qty * S0, qty * P0
    marginA = MARGIN_FRAC * pn0; reserveA = C - sn0 - marginA
    open_fee = pn0 * PERP_TAKER
    fees_est = open_fee + pn0 * PERP_TAKER + net_qty * S0 * SPOT_TAKER
    slip_in = (sn0 + pn0) * bps / 10000
    slip_res_est = slip_in + (net_qty * S0 + qty * P0) * bps / 10000
    if mode == "A":
        margin, reserve = marginA, reserveA
    else:
        extra = max(reserveA - fees_est - slip_res_est, 0.0)
        margin, reserve = marginA + extra, reserveA - extra
    fbt = {t: float(fund[base][t]["realizedRate"]) * qty * mk[t - HOUR] for t in settles}
    mb0 = margin - open_fee
    cum, si, trig, prev = 0.0, 0, None, None
    for e in events:
        while si < len(settles) and settles[si] <= e:
            cum += fbt[settles[si]]; si += 1
        mb = mb0 + cum
        p_liq = (mb + qty * P0) / (qty * (1 + mmr + PERP_TAKER))
        mke = mk[e - HOUR]
        if mke >= p_liq:
            trig = {"e": e, "mk": mke, "p_liq": p_liq, "cum": cum,
                    "equity": mb - qty * (mke - P0), "prev": prev}
            break
        prev = {"e": e, "mk": mke, "p_liq": p_liq}
    out = {"T0": T0, "T1": T1, "S0": S0, "P0": P0, "S1": S1, "P1": P1, "MK0": MK0,
           "contracts": contracts, "qty": qty, "net_qty": net_qty, "margin": margin,
           "reserve": reserve, "fees_est": fees_est, "trig": trig,
           "funding_total": sum(fbt.values()), "days": (T1 - T0) / 86400000}
    if trig:
        e = trig["e"]
        s_exit = sc[e - HOUR]
        proceeds = net_qty * s_exit * (1 - SPOT_TAKER) - net_qty * s_exit * bps / 10000
        eq = trig["equity"]
        out["net0"] = proceeds + max(eq, 0.0) + (reserve - slip_in) - C
        out["netfull"] = proceeds + 0.0 + (reserve - slip_in) - C
        out["funding_until"] = trig["cum"]
    else:
        proceeds = net_qty * S1 * (1 - SPOT_TAKER) - net_qty * S1 * bps / 10000
        margin_final = (mb0 + out["funding_total"] - qty * (P1 - P0)
                        - qty * P1 * PERP_TAKER - qty * P1 * bps / 10000)
        out["net0"] = out["netfull"] = proceeds + margin_final + (reserve - slip_in) - C
    return out

def csv_rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))

BASE_CSV = csv_rows(os.path.join(RES, "benchmark_table.csv"))
def find_row(rows, sym, cap, mode, bps):
    return [r for r in rows if r["symbol"] == sym and int(r["capital"]) == cap
            and r["mode"] == mode and int(r["slippage_bps"]) == bps][0]

# ---------- T-A 现金流水独立核对 ----------
for (sym, cap, mode, bps) in [("BTC", 1000, "B", 5), ("BTC", 1000, "A", 5), ("ETH", 1000, "A", 0)]:
    m = mini(sym, cap, mode, bps)
    row = find_row(BASE_CSV, sym, cap, mode, bps)
    d0 = abs(m["net0"] - float(row["trade_net_liqfee0"]))
    df = abs(m["netfull"] - float(row["trade_net_liqfee_full_equity"]))
    record(f"T-A 现金流水核对 {sym} {cap} {mode} {bps}bp", d0 < TOL and df < TOL,
           f"mini net0={m['net0']:.4f} csv={row['trade_net_liqfee0']} |Δ|={d0:.5f}; "
           f"netfull Δ={df:.5f}")
# ETH/1000/A/0bp 的扣成本区间上界（风点名核对项）
m = mini("ETH", 1000, "A", 0)
ops5 = 5.0 * m["days"] / (365 / 12)
row = find_row(BASE_CSV, "ETH", 1000, "A", 0)
hi_csv = float(row["net_ops5_range"].split("..")[1])
record("T-A ETH/1000/A/0bp 扣成本上界复算", abs((m["net0"] - ops5) - hi_csv) < TOL,
       f"mini={m['net0'] - ops5:+.4f} csv={hi_csv:+.4f}（微小正值，只对诊断口径成立）")

# ---------- T-B 未来价格扰动不变性 ----------
tmpB = tempfile.mkdtemp(prefix="kb_tB_")
dataB, resB = os.path.join(tmpB, "data"), os.path.join(tmpB, "res")
shutil.copytree(DATA, dataB)
m0 = mini("BTC", 1000, "A", 5)
for inst in ["BTC-USDT", "BTC-USDT-SWAP"]:
    p = os.path.join(dataB, f"candles_{inst}.json")
    rows = json.load(open(p))
    for c in rows:
        if int(c[0]) == m0["T1"]:
            c[1] = str(float(c[1]) * 1.5)   # 只改 T1 开盘价（程序的退出成交价来源）
    json.dump(rows, open(p, "w"))
env = dict(os.environ, KB_DATA_DIR=dataB, KB_RES_DIR=resB)
r = subprocess.run([sys.executable, os.path.join(HERE, "benchmark.py")],
                   capture_output=True, text=True, env=env)
rowsB = csv_rows(os.path.join(resB, "benchmark_table.csv")) if r.returncode == 0 else []
alloc_cols = ["contracts", "qty_perp", "margin_posted", "reserve_cash", "fees_est"]
same, diff_net = True, False
for row in [x for x in BASE_CSV if x["symbol"] == "BTC"]:
    rb = find_row(rowsB, "BTC", int(row["capital"]), row["mode"], int(row["slippage_bps"]))
    if any(row[c] != rb[c] for c in alloc_cols):
        same = False
    if row["trade_net_liqfee0"] != rb["trade_net_liqfee0"]:
        diff_net = True
record("T-B 未来价格扰动下开仓配置不变", r.returncode == 0 and same and diff_net,
       f"alloc_identical={same} net_changed(扰动生效)={diff_net} rc={r.returncode}")

# ---------- T-C 成本基准 ----------
row = find_row(BASE_CSV, "BTC", 1000, "A", 5)
m = mini("BTC", 1000, "A", 5)
eq_csv = float(row["margin_equity_left"])
eq_mk0 = m["trig"]["equity"] + m["qty"] * (m["MK0"] - m["P0"])
record("T-C(a) 触发权益按成交价 P0 起算", abs(eq_csv - m["trig"]["equity"]) < TOL
       and abs(eq_csv - eq_mk0) > 0.005,
       f"csv={eq_csv:.4f} P0basis={m['trig']['equity']:.4f} MK0basis={eq_mk0:.4f} "
       f"系统差={m['qty'] * (m['MK0'] - m['P0']):+.5f}")
tmpC = tempfile.mkdtemp(prefix="kb_tC_")
dataC, resC = os.path.join(tmpC, "data"), os.path.join(tmpC, "res")
shutil.copytree(DATA, dataC)
p = os.path.join(dataC, "mark_BTC-USDT-SWAP.json")
rows = json.load(open(p))
for c in rows:
    if int(c[0]) == m["T0"] - HOUR:
        c[4] = str(float(c[4]) * 1.05)      # 只扰动 MK0 的来源：T0 前一根标记收盘
json.dump(rows, open(p, "w"))
env = dict(os.environ, KB_DATA_DIR=dataC, KB_RES_DIR=resC)
r = subprocess.run([sys.executable, os.path.join(HERE, "benchmark.py")],
                   capture_output=True, text=True, env=env)
same_bytes = (r.returncode == 0 and open(os.path.join(resC, "benchmark_table.csv"), "rb").read()
              == open(os.path.join(RES, "benchmark_table.csv"), "rb").read())
record("T-C(b) MK0 扰动不影响主表", same_bytes, f"rc={r.returncode} bytes_identical={same_bytes}")

# ---------- T-D 清算阈值公式边界 ----------
for sym in ["BTC", "ETH"]:
    m = mini(sym, 1000, "A", 5)
    row = find_row(BASE_CSV, sym, 1000, "A", 5)
    t = m["trig"]
    ok = (row["exit_event_utc"] == iso(t["e"]) and t["prev"] is not None
          and t["prev"]["mk"] < t["prev"]["p_liq"] and t["mk"] >= t["p_liq"])
    record(f"T-D 公式边界 {sym}", ok,
           f"trigger={iso(t['e'])} mark={t['mk']:.2f} P_liq={t['p_liq']:.2f}; "
           f"prev {iso(t['prev']['e'])} mark={t['prev']['mk']:.2f} < P_liq={t['prev']['p_liq']:.2f}")

with open(os.path.join(RES, "independent_tests.txt"), "w") as f:
    f.write("independent_tests.py 独立测试结果（v3；与 benchmark 内部自查分开）\n")
    f.write("公式出处：OKX《Forced liquidation FAQ》逐仓 USDT 永续空头清算价 = "
            "(保证金余额 + 面值×张数×开仓均价) / (面值×张数×(1 + MMR + 费率))\n")
    for name, ok, detail in results:
        f.write(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}\n")
    f.write(f"总计 {sum(1 for _, ok, _ in results if ok)}/{len(results)} 通过\n")
n_fail = sum(1 for _, ok, _ in results if not ok)
print(f"independent tests: {len(results) - n_fail}/{len(results)} pass")
sys.exit(1 if n_fail else 0)
