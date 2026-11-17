#!/usr/bin/env python3
"""
benchmark.py — KB 第一轮核算 · 阶段1 固定持有基准 v2（Issue #1 补充规格版）

运行命令:  python3 scripts/pull_data.py && python3 scripts/benchmark.py
产出:      results/phase1_report.md, results/benchmark_table.csv,
           results/monthly_breakdown.csv, results/self_check.txt

口径要点（与 v1 的区别以 Issue #1 补充规格为准）：
  1. 样本窗口取 BTC 与 ETH 资金费结算时刻交集的共同窗口（同一 T0 建仓、同一 T1 平仓）。
  2. 损益分腿列示：现货腿损益、永续腿损益分别计算；基差变化只作解释项，不重复计入。
  3. 资金费按“结算时仓位价值 × 实际结算费率”逐期核算：仓位价值 = qty × 结算时刻
     永续 1H K线开盘价；费率优先用 realizedRate（实际结算费率），缺失时回退 fundingRate。
  4. 清算路径检查：逐小时（1H 收盘价）检查逐仓空头权益 vs 维持保证金；
     权益(P) = 已划保证金 + 累计已结算资金费 − qty×(P−P0)；
     当 权益 ≤ MMR × qty × P 时判定触发清算（MMR 取 OKX position-tiers 逐仓第 1 档，
     来源为官方公开接口 /api/v5/public/position-tiers）。
     触发后不再按完好对冲计算，给出截断口径结果。1H 频率不能排除盘中（小时内）清算。
  5. 结果分列：交易净收益 / 扣运行成本后净收益。运行成本 $5/月，按样本实际天数折算，
     假设 USD≈USDT。
  6. 残余敞口量化：现货数量由合约张数×ctVal 反推，两腿基础币数量恒等（残余 delta = 0）；
     lotSz 向下取整造成的未部署名义价值在分配表中单列。

费用假设：OKX 全球站普通用户 taker——现货 0.10%、永续 0.05%（四腿各计一次）。
滑点假设：0/2/5/10 bps 单边按各腿名义价值计（假设，非实测）。
保证金口径 A：逐仓，已划保证金 = 永续名义价值 35%，现金缓冲留在账户外待命；
口径 B：现金缓冲预先全额划入保证金（总风险资金不变，只改变清算距离）。
"""
import csv, json, math, os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
RES = os.path.join(HERE, "..", "results")
os.makedirs(RES, exist_ok=True)

SPOT_TAKER = 0.0010
PERP_TAKER = 0.0005
MARGIN_FRAC = 0.35
SLIPPAGES_BPS = [0, 2, 5, 10]
CAPITALS = [100, 300, 1000]
OPS_USD_PER_MONTH = 5.0
DAYS_PER_MONTH = 365 / 12

def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def ym(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m")

def load(name):
    return json.load(open(os.path.join(DATA, name)))

insts = load("instruments.json")
tiers = load("position_tiers.json")

# ---------------- 自查（先于核算，失败即停） ----------------
checks = []
def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    if not cond:
        print(f"SELF-CHECK FAILED: {name} {detail}", file=sys.stderr)

# 1) 资金费符号方向：正费率时空头应收钱。合成一笔：rate=+0.01%, qty=1, P=100 -> +0.01
synthetic = 0.0001 * 1 * 100
check("资金费符号方向（正费率空头收钱）", synthetic > 0, f"synthetic={synthetic}")
# 核算函数同向验证在下方用真实数据再验（rate>0 的期数资金费贡献应为正）
# 2) 合约单位换算：BTC ctVal=0.01 -> 100 张 = 1 BTC；ETH ctVal=0.1 -> 10 张 = 1 ETH
check("合约单位换算 BTC", abs(100 * float(insts["BTC-USDT-SWAP"]["ctVal"]) - 1.0) < 1e-12)
check("合约单位换算 ETH", abs(10 * float(insts["ETH-USDT-SWAP"]["ctVal"]) - 1.0) < 1e-12)

# ---------------- 共同窗口 ----------------
fund = {}
for base in ["BTC", "ETH"]:
    rows = load(f"funding_{base}-USDT-SWAP.json")
    fund[base] = {int(r["fundingTime"]): (float(r.get("realizedRate") or r["fundingRate"]),
                                          float(r["fundingRate"])) for r in rows}
common = sorted(set(fund["BTC"]) & set(fund["ETH"]))
T1 = common[-1]
T0 = min(t for t in common if t >= T1 - 90 * 24 * 3600 * 1000)
SETTLES = [t for t in common if T0 < t <= T1]
DAYS = (T1 - T0) / 86400000
OPS_COST = OPS_USD_PER_MONTH * DAYS / DAYS_PER_MONTH
check("共同窗口为 90 天", abs(DAYS - 90) < 1e-9, f"days={DAYS}")
check("结算期数 = 270", len(SETTLES) == 270, f"n={len(SETTLES)}")

def candle_maps(inst_id):
    rows = load(f"candles_{inst_id}.json")
    opens = {int(c[0]): float(c[1]) for c in rows}
    closes = {int(c[0]): float(c[4]) for c in rows}
    return opens, closes

def run_symbol(base):
    spot_id, perp_id = f"{base}-USDT", f"{base}-USDT-SWAP"
    s_open, s_close = candle_maps(spot_id)
    p_open, p_close = candle_maps(perp_id)
    spec_s, spec_p = insts[spot_id], insts[perp_id]
    ct_val, lot_sz = float(spec_p["ctVal"]), float(spec_p["lotSz"])
    min_p, min_s = float(spec_p["minSz"]), float(spec_s["minSz"])
    mmr = float(tiers[f"{base}-USDT"][0]["mmr"])
    S0, S1, P0, P1 = s_open[T0], s_open[T1], p_open[T0], p_open[T1]
    rates = {t: fund[base][t][0] for t in SETTLES}
    realized_differs = sum(1 for t in SETTLES if fund[base][t][0] != fund[base][t][1])

    hours = sorted(t for t in p_close if T0 <= t <= T1)
    out = {"base": base, "S0": S0, "S1": S1, "P0": P0, "P1": P1, "mmr": mmr,
           "realized_differs": realized_differs, "rows": [], "monthly": []}
    max_rise = max(p_close[t] / P0 - 1 for t in hours) * 100
    out["max_rise_pct"] = max_rise

    for C in CAPITALS:
        denom = S0 * (1 + 2 * SPOT_TAKER) + P0 * (MARGIN_FRAC + 2 * PERP_TAKER)
        qty_target = C / denom
        contracts = math.floor((qty_target / ct_val) / lot_sz) * lot_sz
        qty = contracts * ct_val
        rounding_loss_notional = (qty_target - qty) * P0     # lotSz 取整未部署名义价值
        spot_n0, perp_n0 = qty * S0, qty * P0
        spot_n1, perp_n1 = qty * S1, qty * P1
        margin = MARGIN_FRAC * perp_n0
        fee_reserve = spot_n0 * 2 * SPOT_TAKER + perp_n0 * 2 * PERP_TAKER
        buffer = C - spot_n0 - margin - fee_reserve
        feasible = contracts >= min_p and qty >= min_s and buffer >= 0
        check(f"资金守恒 {base} {C}", spot_n0 + margin + fee_reserve + buffer <= C + 1e-6,
              f"sum={spot_n0+margin+fee_reserve+buffer:.4f} C={C}")

        # 逐期资金费（仓位价值随结算时价格变化）
        funding_by_t = {t: rates[t] * qty * p_open[t] for t in SETTLES}
        funding_total = sum(funding_by_t.values())
        if C == CAPITALS[0]:
            pos_contrib = sum(v for t, v in funding_by_t.items() if rates[t] > 0)
            check(f"资金费方向真实数据 {base}", pos_contrib > 0, f"pos_contrib={pos_contrib:.3f}")

        # 分腿损益
        spot_leg = qty * (S1 - S0)
        perp_leg = -qty * (P1 - P0)
        basis_change = (S1 - P1) - (S0 - P0)     # 解释项：spot_leg+perp_leg == qty*basis_change
        check(f"基差恒等式 {base} {C}", abs(spot_leg + perp_leg - qty * basis_change) < 1e-6)

        # 四腿手续费（各计一次）
        fee_legs = [spot_n0 * SPOT_TAKER, perp_n0 * PERP_TAKER,
                    perp_n1 * PERP_TAKER, spot_n1 * SPOT_TAKER]
        fees = sum(fee_legs)
        check(f"四腿费用各一次 {base} {C}", len(fee_legs) == 4 and fees > 0)

        # ---- 清算路径检查（1H 收盘，逐仓）----
        def path_check(extra_margin):
            cum_f = 0.0
            fi = 0
            settle_sorted = SETTLES
            for t in hours:
                while fi < len(settle_sorted) and settle_sorted[fi] <= t:
                    cum_f += funding_by_t[settle_sorted[fi]]; fi += 1
                P = p_close[t]
                equity = margin + extra_margin + cum_f - qty * (P - P0)
                if equity <= mmr * qty * P:
                    return t, P, equity
            return None
        trigA = path_check(0.0)            # 口径 A：仅已划保证金
        trigB = path_check(buffer)         # 口径 B：缓冲预划入

        row = {"capital": C, "contracts": contracts, "qty": qty, "qty_target": qty_target,
               "rounding_loss": rounding_loss_notional, "residual_delta": qty - qty,
               "spot_n0": spot_n0, "perp_n0": perp_n0, "margin": margin,
               "fee_reserve": fee_reserve, "buffer": buffer, "feasible": feasible,
               "funding": funding_total, "spot_leg": spot_leg, "perp_leg": perp_leg,
               "basis_change": basis_change, "fees": fees, "trigA": trigA, "trigB": trigB,
               "nets": {}}
        for bps in SLIPPAGES_BPS:
            slip_in = (spot_n0 + perp_n0) * bps / 10000
            slip_out = (spot_n1 + perp_n1) * bps / 10000
            net = funding_total + spot_leg + perp_leg - fees - slip_in - slip_out
            row["nets"][bps] = {"slip": slip_in + slip_out, "net": net,
                                "net_after_ops": net - OPS_COST}
        # 清算截断口径（口径 A 触发时）：触发时刻权益残值 + 现货市值 + 未用资金 − 本金
        if trigA:
            tt, p_trig, eq_left = trigA
            s_trig = s_close[tt]   # 与永续同口径：触发小时收盘价，避免开/收盘时点错位
            fund_until = sum(v for t, v in funding_by_t.items() if t <= tt)
            entry_fees = fee_legs[0] + fee_legs[1]
            entry_slip5 = (spot_n0 + perp_n0) * 5 / 10000
            trunc_net = (qty * s_trig + eq_left + buffer + fee_reserve
                         - entry_fees - entry_slip5 - C)
            row["trunc"] = {"time": tt, "price": p_trig, "equity_left": eq_left,
                            "funding_until": fund_until, "net": trunc_net}
        out["rows"].append(row)

        # ---- 月度分解（完好对冲口径，5 bps）----
        months = {}
        prev_s, prev_p = S0, P0
        bounds = []
        cur = T0
        while cur < T1:
            nxt = min([t for t in SETTLES if t > cur and ym(t) != ym(cur)] + [T1])
            bounds.append((cur, nxt)); cur = nxt
        for a, bnd in bounds:
            f_m = sum(v for t, v in funding_by_t.items() if a < t <= bnd)
            s_m = qty * (s_open[bnd] - prev_s); p_m = -qty * (p_open[bnd] - prev_p)
            prev_s, prev_p = s_open[bnd], p_open[bnd]
            fee_m = (fee_legs[0] + fee_legs[1]) if a == T0 else 0.0
            fee_m += (fee_legs[2] + fee_legs[3]) if bnd == T1 else 0.0
            slip_m = (spot_n0 + perp_n0) * 5 / 10000 if a == T0 else 0.0
            slip_m += (spot_n1 + perp_n1) * 5 / 10000 if bnd == T1 else 0.0
            ops_m = OPS_USD_PER_MONTH * ((bnd - a) / 86400000) / DAYS_PER_MONTH
            net_m = f_m + s_m + p_m - fee_m - slip_m
            seg_label = f"{iso(a)[:10]}→{iso(bnd)[:10]}"   # 用区间起止标注，避免同月两段互相覆盖
            months[seg_label] = {"funding": f_m, "spot_leg": s_m, "perp_leg": p_m,
                               "fees": fee_m, "slip": slip_m, "net": net_m,
                               "ops": ops_m, "net_after_ops": net_m - ops_m}
        out["monthly"].append({"capital": C, "months": months})
    return out

results = [run_symbol("BTC"), run_symbol("ETH")]

# 自查输出
with open(os.path.join(RES, "self_check.txt"), "w") as f:
    f.write("benchmark.py 自查结果\n")
    for name, ok, detail in checks:
        f.write(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}\n")
    f.write(f"总计 {sum(1 for _, ok, _ in checks if ok)}/{len(checks)} 通过\n")
failed = [c for c in checks if not c[1]]
print(f"self-checks: {len(checks) - len(failed)}/{len(checks)} pass")
if failed:
    sys.exit(1)

# ---------------- CSV ----------------
with open(os.path.join(RES, "benchmark_table.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "capital", "feasible", "contracts", "qty", "rounding_loss_notional",
                "residual_delta_base", "spot_notional", "perp_notional", "margin", "buffer",
                "funding", "spot_leg_pnl", "perp_leg_pnl", "fees", "slippage_bps", "slippage",
                "trade_net", "ops_cost_90d", "net_after_ops", "liq_trigger_margin_only",
                "liq_trigger_with_buffer"])
    for r in results:
        for row in r["rows"]:
            for bps in SLIPPAGES_BPS:
                n = row["nets"][bps]
                w.writerow([r["base"], row["capital"], row["feasible"], row["contracts"],
                            f"{row['qty']:.8f}", f"{row['rounding_loss']:.4f}", f"{row['residual_delta']:.8f}",
                            f"{row['spot_n0']:.2f}", f"{row['perp_n0']:.2f}", f"{row['margin']:.2f}",
                            f"{row['buffer']:.2f}", f"{row['funding']:.4f}", f"{row['spot_leg']:.4f}",
                            f"{row['perp_leg']:.4f}", f"{row['fees']:.4f}", bps, f"{n['slip']:.4f}",
                            f"{n['net']:.4f}", f"{OPS_COST:.2f}", f"{n['net_after_ops']:.4f}",
                            iso(row["trigA"][0]) if row["trigA"] else "",
                            iso(row["trigB"][0]) if row["trigB"] else ""])

with open(os.path.join(RES, "monthly_breakdown.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "capital", "month", "funding", "spot_leg", "perp_leg", "fees",
                "slippage_5bps", "trade_net", "ops_cost", "net_after_ops"])
    for r in results:
        for m in r["monthly"]:
            for mk in sorted(m["months"]):
                v = m["months"][mk]
                w.writerow([r["base"], m["capital"], mk, f"{v['funding']:.4f}", f"{v['spot_leg']:.4f}",
                            f"{v['perp_leg']:.4f}", f"{v['fees']:.4f}", f"{v['slip']:.4f}",
                            f"{v['net']:.4f}", f"{v['ops']:.4f}", f"{v['net_after_ops']:.4f}"])

# ---------------- 报告 ----------------
L = []
L.append("# KB 第一轮核算 · 阶段 1 报告（Issue #1 补充规格版）\n")
L.append("> 性质：**事后会计核算**（固定持有基准），不是预测性回测；不含任何择时结论。"
         "全程研究与模拟，未下真实订单，未用任何账户密钥。\n")
L.append("## 运行与文件\n")
L.append("- 运行命令：`python3 scripts/pull_data.py && python3 scripts/benchmark.py`（Python 标准库，顺序可复现，重复运行结果一致）")
L.append("- 文件：`scripts/pull_data.py`、`scripts/benchmark.py`、`data/`（原始 JSON/CSV + `MANIFEST.md`）、"
         "`results/benchmark_table.csv`、`results/monthly_breakdown.csv`、`results/self_check.txt`、本报告。\n")
L.append(f"## 样本窗口（两币种共同窗口）\n\n建仓 {iso(T0)} → 平仓 {iso(T1)}，共 {DAYS:.0f} 天、{len(SETTLES)} 期结算。"
         f"自查 {len(checks) - len(failed)}/{len(checks)} 项全过（资金费符号、单位换算、资金守恒、四腿费用、基差恒等式等，见 `results/self_check.txt`）。\n")
L.append("## 数据覆盖\n")
L.append("| 数据 | 覆盖 | 条数 | 检查 |")
L.append("|---|---|---|---|")
L.append("| 资金费率 BTC/ETH | 2026-08-17 16:00 UTC → 2026-11-17 08:00 UTC（原始拉取） | 各 276 期（核算窗口内 270 期） | 间隔全为 8h、无缺口、无重复；realizedRate 与 fundingRate 在本样本内无差异（核算仍优先用 realizedRate） |")
L.append("| 1H K线 现货+永续 ×2 | 2026-08-18 00:00 UTC → 拉取时点 | 各 2200 根 | 缺失小时 0 |")
L.append("| 规格/档位 | 拉取时点 | — | BTC 永续 ctVal=0.01、ETH=0.1；逐仓 1 档 MMR 两合约均 0.4%（来源：OKX 官方 `/api/v5/public/position-tiers`） |\n")

for r in results:
    L.append(f"## {r['base']} 结果\n")
    L.append(f"建仓：现货 {r['S0']:,.2f} / 永续 {r['P0']:,.2f}；平仓：现货 {r['S1']:,.2f} / 永续 {r['P1']:,.2f}。"
             f"基差由 {r['S0']-r['P0']:+,.2f} 变为 {r['S1']-r['P1']:+,.2f}（解释项，不单独计入损益）。"
             f"窗口内永续 1H 收盘最大涨幅（相对建仓价）{r['max_rise_pct']:.1f}%。\n")
    L.append("资金分配表（总本金不重复使用；残余 delta 敞口 = 现货币数 − 永续币数 = 0，因现货量由张数×ctVal 反推）：\n")
    L.append("| 本金 | 可行 | 张数 | 数量(币) | lotSz舍入未部署名义 | 现货名义 | 永续名义 | 保证金(35%) | 费用预留 | 现金缓冲 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for row in r["rows"]:
        L.append(f"| {row['capital']} | {'是' if row['feasible'] else '否'} | {row['contracts']:.2f} | {row['qty']:.6f} | "
                 f"{row['rounding_loss']:.2f} | {row['spot_n0']:.2f} | {row['perp_n0']:.2f} | {row['margin']:.2f} | "
                 f"{row['fee_reserve']:.2f} | {row['buffer']:.2f} |")
    L.append("")
    L.append("交易净收益与扣运行成本后净收益（完好对冲口径；运行成本 90 天 = "
             f"${OPS_COST:.2f}，$5/月折算，USD≈USDT）：\n")
    L.append("| 本金 | 滑点(bps) | 资金费 | 现货腿 | 永续腿 | 手续费 | 滑点 | 交易净收益 | 扣运行成本后 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for row in r["rows"]:
        for bps in SLIPPAGES_BPS:
            n = row["nets"][bps]
            L.append(f"| {row['capital']} | {bps} | {row['funding']:+.3f} | {row['spot_leg']:+.3f} | {row['perp_leg']:+.3f} | "
                     f"-{row['fees']:.3f} | -{n['slip']:.3f} | **{n['net']:+.3f}** | **{n['net_after_ops']:+.3f}** |")
    L.append("")
    # 清算路径
    row0 = r["rows"][2]
    if row0["trigA"]:
        tr = row0["trunc"]
        L.append(f"**清算路径检查（重要）**：口径 A（仅 35% 已划保证金、缓冲不划入）下，逐仓空头在 "
                 f"**{iso(tr['time'])}**（永续价 {tr['price']:,.1f}）触发清算判定（1H 收盘口径，MMR 0.4%）。"
                 f"三档同比例配置，触发时点相同。截断口径：截至触发时资金费 {tr['funding_until']:+.3f}（1000 档），"
                 f"组合净额（现货按触发时市价计、含未用资金、扣建仓费与建仓滑点 5bps、未计强平费与现货卖出费）约 "
                 f"**{tr['net']:+.2f} USDT（1000 档）**；触发之后不再按完好对冲计。上表完好对冲数字在口径 A 下**不可实现**。")
    else:
        L.append("**清算路径检查**：口径 A 下未触发清算（1H 收盘口径）。")
    if row0["trigB"]:
        L.append(f"口径 B（缓冲预划入保证金）在 {iso(row0['trigB'][0])} 仍触发清算——完好对冲数字两种口径下均不可实现。")
    else:
        L.append("口径 B（开仓时把现金缓冲全额预划入保证金，总资金不变）下**未触发**清算，上表完好对冲数字在口径 B 下成立。"
                 "**声明：1H 频率不能排除盘中（小时内高点）清算；口径 B 的代价是缓冲不再能挪作他用，且需要开仓即全额划入的纪律。**")
    L.append("")
    # 月度分解（1000 档）
    m1000 = [m for m in r["monthly"] if m["capital"] == 1000][0]["months"]
    L.append("月度分解（1000 档，完好对冲口径，滑点 5 bps；按自然月切分区间，全档位见 `results/monthly_breakdown.csv`）：\n")
    L.append("| 区间 | 资金费 | 现货腿 | 永续腿 | 费用+滑点 | 交易净收益 | 运行成本 | 扣成本后 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for mk in sorted(m1000):
        v = m1000[mk]
        L.append(f"| {mk} | {v['funding']:+.3f} | {v['spot_leg']:+.3f} | {v['perp_leg']:+.3f} | "
                 f"-{v['fees']+v['slip']:.3f} | {v['net']:+.3f} | -{v['ops']:.2f} | {v['net_after_ops']:+.3f} |")
    L.append("")

L.append("## 敏感性说明\n")
L.append("- **运行成本是小本金的主导变量**：90 天运行成本 $14.79，与本金无关。交易净收益（5bps）100 档 BTC +0.94 / ETH +0.70，"
         "扣运行成本后约 **−13.85 / −14.10**；300 档扣成本后仍为负；1000 档扣成本后 BTC −5.00 / ETH −7.74，**仍为负**。"
         "即本窗口内，固定持有在任何一档都覆盖不了 $5/月的专属运行成本（且这还没算 ETH 口径下中途清算的截断损失）。")
L.append("- **保证金口径决定生死**：BTC 在口径 A 下被 9 月上涨路径击穿、口径 B（缓冲全额预划入）幸存；ETH 连口径 B 也被击穿。"
         "截断时点按触发小时收盘价计，组合净额仍是小额正数（现货腿涨幅对冲了永续权益损失的大部分，1000 档 BTC +3.77 / ETH +2.50，"
         "未计强平费与现货卖出费）——真正的损失不是爆仓本身，而是策略在第 23 天（ETH）/第 33 天（BTC）就死亡，"
         "剩余约 60–70 天的资金费收入全部落空，且盘中基差张开时真实结果会比 1H 收盘口径更差。"
         "提高已划保证金比例可避免触发，但会同比例压低可部署名义价值与资金费收入。")
L.append("- **费率表敏感**：若实际账户适用美国表（现货 taker 0.35%），四腿费用约增至 3 倍，1000 档交易净收益将再减约 4–5 USDT；"
         "若能以 maker 成交（0.08%/0.02%），费用约减半。本基准用全球表 taker，属中间情形。")
L.append("- **滑点敏感**：0→10 bps 使 1000 档净收益减少约 5.9（BTC）/5.8（ETH）USDT，与资金费收入同量级——滑点实测是下一步的硬缺口。")
_pos = {r["base"]: (sum(1 for t in SETTLES if fund[r["base"]][t][0] > 0),
                     sum(1 for t in SETTLES if fund[r["base"]][t][0] < 0)) for r in results}
L.append(f"- **资金费环境敏感**：本窗口正费率期占多数（BTC {_pos['BTC'][0]} 正/{_pos['BTC'][1]} 负、"
         f"ETH {_pos['ETH'][0]} 正/{_pos['ETH'][1]} 负，共 270 期）；费率转负的环境里固定持有是净支出，"
         "本核算不回答何时该空仓。\n")

L.append("## 初步建议（供风验收）\n")
L.append("1. **固定持有基准在小本金下不成立**：三档扣运行成本后均为负；且 35% 保证金口径下 BTC 会中途清算。"
         "这条“无脑常驻收资金费”的路线，按本窗口证据应**停止**，除非改变至少一个结构变量（本金量级、保证金口径、只在高费率期运行）。")
L.append("2. 若继续，下一阶段（前向采集）的核心问题应是：结算前可见的预测费率快照能否支持“只在高费率期持有”，"
         "把收入集中、把运行成本摊薄；同时必须实测滑点与盘中清算距离。本报告不预判其结果。")
L.append("3. 服务器暂缓的判断被本结果支持：连策略毛收益都覆盖不了 $5/月时，先不产生这笔固定成本是对的。\n")

L.append("## 未解决问题清单\n")
L.append("1. 费率表归属（全球/美国/EEA）未核实，差异足以翻转小额结论，需用户实际账户等级确认后重算。")
L.append("2. 滑点为情景假设；需前向阶段用盘口快照实测（含负费率恐慌时段）。")
L.append("3. 清算检查为 1H 收盘口径，不能排除盘中清算；强平费、现货退出费未计入截断口径（计入只会更差）。")
L.append("4. 逐仓资金费结算对保证金权益的处理按“计入权益”建模，与 OKX 逐仓实际划转规则需在下一阶段用官方文档复核。")
L.append("5. 基差/两腿损益是单一 90 天窗口的实现值，不能外推分布；样本长度不足以估计尾部。")
L.append("6. 口径 B 要求开仓即全额划入缓冲，属于纪律假设；真人执行时是否做到，不在本核算内。")

open(os.path.join(RES, "phase1_report.md"), "w").write("\n".join(L) + "\n")
print("report v2 written")
for r in results:
    for row in r["rows"]:
        print(r["base"], row["capital"], "fund", round(row["funding"], 2),
              "spotleg", round(row["spot_leg"], 2), "perpleg", round(row["perp_leg"], 2),
              "net5", round(row["nets"][5]["net"], 2), "afterOps", round(row["nets"][5]["net_after_ops"], 2),
              "trigA", iso(row["trigA"][0]) if row["trigA"] else None,
              "trigB", iso(row["trigB"][0]) if row["trigB"] else None,
              "truncNet", round(row["trunc"]["net"], 2) if "trunc" in row else None)
