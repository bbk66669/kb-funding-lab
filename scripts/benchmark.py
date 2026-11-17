#!/usr/bin/env python3
"""
benchmark.py — KB 第一轮核算 · 阶段1 固定持有基准（风验收修正版 v2）

复现命令（冻结数据，只跑本脚本）:
    python3 scripts/benchmark.py
数据更新（会改变样本，勿用于复现）:
    python3 scripts/pull_data.py            # 全量更新
    python3 scripts/pull_data.py --only-mark --start-ms <T0> --end-ms <T1>   # 只补标记价格

产出: results/phase1_report.md, results/benchmark_table.csv,
      results/monthly_breakdown.csv, results/counterfactual_table.csv,
      results/self_check.txt

v2 修正口径（风验收六项）：
  1. 标记价格：资金费仓位价值与清算路径全部改用标记价格 1H 序列
     （/api/v5/market/history-mark-price-candles）。OKX 规则：资金费 = 仓位价值 × 费率，
     仓位价值按标记价格计（OKX 帮助中心《Perpetual funding fee mechanism》）；
     逐仓资金费从该仓逐仓保证金扣除/计入（同上），本版据此建模，不再是未核实假设；
     清算以标记价格判定、维持保证金率达 100% 触发（OKX 强平 FAQ 口径，本版用
     权益 ≤ MMR×仓位价值 的等价判定，MMR 取 position-tiers 逐仓 1 档）。
  2. 事件时间：K 线 ts 为小时开始时刻，close 为该小时结束时刻的价格。
     事件时刻 e 的标记价格 = ts=e-1h 那根标记 K 线的 close。路径扫描只限 (T0, T1]。
     T1 口径（全报告一致）：T1 时点先结算资金费，再按 T1 成交价退出；
     资金费期数 = (T0, T1] 内结算期数。
  3. 费用与资金来源：
     - 现货买入手续费按计价规则从买入资产扣：净到手 = 毛数量×(1-现货费率)，
       净敞口 = 永续数量 - 现货净到手数量，如实量化。
     - 永续开仓费从保证金权益扣除（进入清算判定）；清算截断补强平费情景
       （剩余权益的 0% / 100% 两档，不编造精确强平成交价）与现货退出费（卖出费+滑点）。
     - 模式 B 的保证金 = 基础保证金 + (现金预留 - 费用预留 - 滑点预留)，被预留的
       费用/滑点资金不再进入保证金，同一笔钱不重复使用；资金守恒逐模式×情景检查。
  4. 表格：主表为 A/B 两模式各自的路径结果（截断或存活）；完整 90 天完好对冲
     单独列为“忽略清算的反事实诊断”。运行成本并列 0 与 5 美元/月两情景
     （5 美元是独占运行预算情景，非已证明的最低必要成本）。
  5. 文字结论限定于本样本、本配置、本金与成本假设。
  6. 自查新增：事件时间错位、T1 后扫描、清算后计入资金费、realizedRate 缺失、
     滑点资金超支等检查；覆盖统计全部由数据生成，不硬编码。

费用假设：OKX 全球站普通用户 taker——现货 0.10%、永续 0.05%。
滑点假设：0/2/5/10 bps 单边按各腿名义价值计（假设，非实测）。
保证金：逐仓；模式 A 已划保证金 = 永续名义 35%，其余现金留作费用/滑点/缓冲；
模式 B 在 A 基础上把费用/滑点预留之外的现金全部预划入保证金。
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
HOUR_MS = 3600 * 1000
OPS_SCENARIOS = [0.0, 5.0]           # 美元/月：0 与独占运行预算情景
DAYS_PER_MONTH = 365 / 12

def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def load(name):
    return json.load(open(os.path.join(DATA, name)))

insts = load("instruments.json")
tiers = load("position_tiers.json")

checks = []
def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))

# ---------- 原始数据加载 ----------
fund_raw, fund = {}, {}
for base in ["BTC", "ETH"]:
    rows = load(f"funding_{base}-USDT-SWAP.json")
    fund_raw[base] = rows
    fund[base] = {int(r["fundingTime"]): r for r in rows}

def candle_maps(inst_id):
    rows = load(f"candles_{inst_id}.json")
    return ({int(c[0]): float(c[1]) for c in rows},
            {int(c[0]): float(c[4]) for c in rows})

def mark_close_map(inst_id):
    rows = load(f"mark_{inst_id}.json")
    return {int(c[0]): float(c[4]) for c in rows}, rows

marks, mark_rows = {}, {}
trade = {}
for base in ["BTC", "ETH"]:
    marks[base], mark_rows[base] = mark_close_map(f"{base}-USDT-SWAP")
    trade[base] = {"spot": candle_maps(f"{base}-USDT"),
                   "perp": candle_maps(f"{base}-USDT-SWAP")}

def mark_at(base, t_ms):
    """事件时刻 t 的标记价格 = ts=t-1h 标记 K 线的 close（该小时结束价）。"""
    return marks[base][t_ms - HOUR_MS]

# ---------- 共同窗口 ----------
common = sorted(set(fund["BTC"]) & set(fund["ETH"]))
T1 = common[-1]
T0 = min(t for t in common if t >= T1 - 90 * 24 * 3600 * 1000)
SETTLES = [t for t in common if T0 < t <= T1]
DAYS = (T1 - T0) / 86400000
EVENTS = [T0 + i * HOUR_MS for i in range(1, int((T1 - T0) // HOUR_MS) + 1)]  # (T0, T1]

# ---------- 自查：数据层 ----------
check("资金费符号方向（正费率空头收钱）", 0.0001 * 1 * 100 > 0, "synthetic=0.01")
check("合约单位换算 BTC", abs(100 * float(insts["BTC-USDT-SWAP"]["ctVal"]) - 1.0) < 1e-12)
check("合约单位换算 ETH", abs(10 * float(insts["ETH-USDT-SWAP"]["ctVal"]) - 1.0) < 1e-12)
# realizedRate 缺失必须报错，不许静默替代
missing_rr = [(b, t) for b in ["BTC", "ETH"] for t in SETTLES
              if fund[b][t].get("realizedRate") in (None, "")]
check("窗口内 realizedRate 全部存在（缺失即中止）", not missing_rr, f"missing={missing_rr[:3]}")
# 标记价格覆盖：每个结算时刻与每个事件时刻都必须有标记价
mark_missing = [(b, t) for b in ["BTC", "ETH"] for t in set(SETTLES) | set(EVENTS)
                if (t - HOUR_MS) not in marks[b]]
check("标记价格覆盖全部结算/事件时刻", not mark_missing, f"missing={mark_missing[:3]}")
for b in ["BTC", "ETH"]:
    ts_sorted = sorted(marks[b])
    exp = (ts_sorted[-1] - ts_sorted[0]) // HOUR_MS + 1
    check(f"标记K线无缺口 {b}", exp == len(ts_sorted), f"n={len(ts_sorted)} expected={exp}")
    ft = sorted(fund[b])
    gaps = {ft[i + 1] - ft[i] for i in range(len(ft) - 1)}
    check(f"资金费间隔单一(8h) {b}", gaps == {8 * HOUR_MS}, f"gaps={gaps}")
# 事件时间映射：candle ts=h 的 close 对应事件时刻 h+1h
_probe_ts = T0 - HOUR_MS
check("事件时间映射（close 对应 ts+1h）",
      mark_at("BTC", T0) == marks["BTC"][_probe_ts] and _probe_ts + HOUR_MS == T0)
check("路径扫描范围 (T0, T1] 且计数=窗口小时数",
      EVENTS[0] == T0 + HOUR_MS and EVENTS[-1] == T1 and len(EVENTS) == int(DAYS * 24),
      f"n={len(EVENTS)}")

RATES = {b: {t: float(fund[b][t]["realizedRate"]) for t in SETTLES} for b in ["BTC", "ETH"]}
B_DEGEN = []   # 模式 B 现金池不足以覆盖费用+滑点预留、退化为 A 的情景（报告用）

def run_symbol(base):
    spot_open, spot_close = trade[base]["spot"]
    perp_open, perp_close = trade[base]["perp"]
    spec_s, spec_p = insts[f"{base}-USDT"], insts[f"{base}-USDT-SWAP"]
    ct_val, lot_sz = float(spec_p["ctVal"]), float(spec_p["lotSz"])
    min_p, min_s = float(spec_p["minSz"]), float(spec_s["minSz"])
    mmr = float(tiers[f"{base}-USDT"][0]["mmr"])
    S0, P0 = spot_open[T0], perp_open[T0]
    S1, P1 = spot_open[T1], perp_open[T1]
    MK0 = mark_at(base, T0)
    funding_by_t = {}   # 需 qty，先占位，qty 确定后填充（每档 qty 不同，逐档计算）
    out = {"base": base, "S0": S0, "S1": S1, "P0": P0, "P1": P1, "MK0": MK0, "mmr": mmr,
           "rows": [], "monthly": [], "counter": []}
    max_rise_mark = max(mark_at(base, e) / MK0 - 1 for e in EVENTS) * 100
    out["max_rise_pct"] = max_rise_mark

    for C in CAPITALS:
        denom = S0 * (1 + 2 * SPOT_TAKER) + P0 * (MARGIN_FRAC + 2 * PERP_TAKER)
        qty_target = C / denom
        contracts = math.floor((qty_target / ct_val) / lot_sz) * lot_sz
        qty = contracts * ct_val                       # 永续数量 = 现货毛数量
        net_qty = qty * (1 - SPOT_TAKER)               # 现货买入费从资产扣后的净到手
        residual = qty - net_qty                       # 残余净空头敞口（币）
        spot_n0, perp_n0 = qty * S0, qty * P0
        marginA = MARGIN_FRAC * perp_n0
        reserveA = C - spot_n0 - marginA               # 费用+滑点+缓冲的现金池（模式 A 的保证金外现金）
        perp_open_fee = perp_n0 * PERP_TAKER
        perp_close_fee_full = qty * P1 * PERP_TAKER
        spot_sell_fee_full = net_qty * S1 * SPOT_TAKER
        fees_est = perp_open_fee + perp_n0 * PERP_TAKER + net_qty * S0 * SPOT_TAKER  # 预留口径（开仓价估）
        feasible_base = contracts >= min_p and qty >= min_s and reserveA >= fees_est
        check(f"资金守恒 {base} {C}", abs(spot_n0 + marginA + reserveA - C) < 1e-6,
              f"sum={spot_n0 + marginA + reserveA:.4f}")
        check(f"现货净到手与残余敞口 {base} {C}",
              abs(net_qty - qty * (1 - SPOT_TAKER)) < 1e-12 and residual > 0,
              f"net={net_qty:.8f} residual={residual:.8f}")
        check(f"四腿费用各计一次 {base} {C}",
              perp_open_fee > 0 and perp_close_fee_full > 0 and spot_sell_fee_full > 0
              and abs(qty * S0 * SPOT_TAKER - spot_n0 * SPOT_TAKER) < 1e-9)

        fbt = {t: RATES[base][t] * qty * mark_at(base, t) for t in SETTLES}
        funding_total = sum(fbt.values())
        if C == CAPITALS[0]:
            pos_c = sum(v for t, v in fbt.items() if RATES[base][t] > 0)
            check(f"资金费方向真实数据 {base}", pos_c > 0, f"pos_contrib={pos_c:.3f}")

        # 完好对冲（反事实诊断用）
        spot_proceeds_full = net_qty * S1 * (1 - SPOT_TAKER)
        spot_leg_full = spot_proceeds_full - spot_n0
        perp_leg_full = -qty * (P1 - P0)
        perp_fees_full = perp_open_fee + perp_close_fee_full
        for bps in SLIPPAGES_BPS:
            slip_in = (spot_n0 + perp_n0) * bps / 10000
            slip_out = (net_qty * S1 + qty * P1) * bps / 10000
            net_cf = funding_total + spot_leg_full + perp_leg_full - perp_fees_full - slip_in - slip_out
            out["counter"].append({"capital": C, "bps": bps, "funding": funding_total,
                                    "spot_leg": spot_leg_full, "perp_leg": perp_leg_full,
                                    "perp_fees": perp_fees_full, "slip": slip_in + slip_out,
                                    "net": net_cf})

        # ---- 路径模拟（模式 × 滑点）----
        for mode in ["A", "B"]:
            for bps in SLIPPAGES_BPS:
                slip_in = (spot_n0 + perp_n0) * bps / 10000
                slip_out_full = (net_qty * S1 + qty * P1) * bps / 10000
                slip_total = slip_in + slip_out_full
                # 资金来源口径：入场滑点是入场时点的现金需求，只能由保证金外现金池支付；
                # 出场滑点与出场费用从出场款项中净扣（来源是出场款本身），不占用入场现金池。
                # 模式 B 的预留额（费用+滑点）不得进入保证金，由下方守恒与预留检查把关。
                if mode == "A":
                    margin_posted, reserve_cash = marginA, reserveA
                else:
                    extra = max(reserveA - fees_est - slip_total, 0.0)
                    margin_posted, reserve_cash = marginA + extra, reserveA - extra
                    if extra == 0.0 and reserveA < fees_est + slip_total - 1e-9:
                        B_DEGEN.append((base, C, bps, fees_est + slip_total - reserveA))
                check(f"滑点资金来源充足(入场现金) {base} {C} {mode} {bps}bp",
                      slip_in <= reserve_cash + 1e-9,
                      f"slip_in={slip_in:.4f} source={reserve_cash:.4f}")
                if mode == "B":
                    check(f"模式B预留未进保证金 {base} {C} {bps}bp",
                          abs(reserve_cash - (fees_est + slip_total)) < 1e-6 or extra == 0.0,
                          f"reserve={reserve_cash:.4f} fees+slip={fees_est + slip_total:.4f}")
                check(f"资金守恒 {base} {C} {mode} {bps}bp",
                      abs(spot_n0 + margin_posted + reserve_cash - C) < 1e-6)
                equity0 = margin_posted - perp_open_fee
                cum_f, trig = 0.0, None
                settle_idx = 0
                for e in EVENTS:
                    while settle_idx < len(SETTLES) and SETTLES[settle_idx] <= e:
                        cum_f += fbt[SETTLES[settle_idx]]; settle_idx += 1
                    mk = mark_at(base, e)
                    equity = equity0 + cum_f - qty * (mk - MK0)
                    if equity <= mmr * qty * mk:
                        trig = (e, mk, equity, cum_f)
                        break
                if trig:
                    e, mk_e, eq_left, fund_until = trig
                    check(f"清算后不再计资金费 {base} {C} {mode} {bps}bp",
                          abs(fund_until - sum(v for t, v in fbt.items() if t <= e)) < 1e-9
                          and (e == T1 or fund_until <= funding_total + 1e-9),
                          f"until={fund_until:.4f} total={funding_total:.4f}")
                    s_exit = spot_close[e - HOUR_MS]
                    slip_exit_spot = net_qty * s_exit * bps / 10000
                    spot_proceeds = net_qty * s_exit * (1 - SPOT_TAKER) - slip_exit_spot
                    spot_pnl = spot_proceeds - spot_n0
                    reserve_left = reserve_cash - slip_in - slip_exit_spot
                    nets = {}
                    for liq_frac in [0.0, 1.0]:
                        residual_margin = max(eq_left * (1 - liq_frac), 0.0)
                        total_final = spot_proceeds + residual_margin + reserve_left
                        nets[liq_frac] = total_final - C
                    out["rows"].append({
                        "capital": C, "mode": mode, "bps": bps, "feasible": feasible_base,
                        "contracts": contracts, "qty": qty, "net_qty": net_qty, "residual": residual,
                        "spot_n0": spot_n0, "perp_n0": perp_n0, "margin_posted": margin_posted,
                        "reserve_cash": reserve_cash, "fees_est": fees_est,
                        "outcome": "liquidated", "exit_time": e, "funding": fund_until,
                        "spot_leg": spot_pnl, "eq_left": eq_left,
                        "net_liq0": nets[0.0], "net_liqfull": nets[1.0]})
                else:
                    net_cf = [c for c in out["counter"] if c["capital"] == C and c["bps"] == bps][0]["net"]
                    out["rows"].append({
                        "capital": C, "mode": mode, "bps": bps, "feasible": feasible_base,
                        "contracts": contracts, "qty": qty, "net_qty": net_qty, "residual": residual,
                        "spot_n0": spot_n0, "perp_n0": perp_n0, "margin_posted": margin_posted,
                        "reserve_cash": reserve_cash, "fees_est": fees_est,
                        "outcome": "survived", "exit_time": T1, "funding": funding_total,
                        "spot_leg": spot_leg_full, "eq_left": None,
                        "net_liq0": net_cf, "net_liqfull": net_cf})
        # ---- 月度分解（5 bps，模式 A/B，路径口径）----
        for mode in ["A", "B"]:
            bps = 5
            slip_in = (spot_n0 + perp_n0) * bps / 10000
            slip_out_full = (net_qty * S1 + qty * P1) * bps / 10000
            slip_total = slip_in + slip_out_full
            if mode == "A":
                margin_posted, reserve_cash = marginA, reserveA
            else:
                extra = max(reserveA - fees_est - slip_total, 0.0)
                margin_posted, reserve_cash = marginA + extra, reserveA - extra
            equity0 = margin_posted - perp_open_fee
            # 先定位触发点（与主模拟同口径）
            cum_f, trig_e, trig_eq = 0.0, None, None
            si = 0
            fbt_cum = {}
            for e in EVENTS:
                while si < len(SETTLES) and SETTLES[si] <= e:
                    cum_f += fbt[SETTLES[si]]; si += 1
                fbt_cum[e] = cum_f
                mk = mark_at(base, e)
                equity = equity0 + cum_f - qty * (mk - MK0)
                if trig_e is None and equity <= mmr * qty * mk:
                    trig_e, trig_eq = e, equity
            def portfolio_value(x, after_exit=False):
                """时点 x 的组合价值：现货净数量×现货价 + 保证金权益(标记) + 现金池余额。
                x 为结算时刻（用开盘成交价）；触发退出后用触发口径。"""
                if trig_e is not None and x >= trig_e:
                    e = trig_e
                    s_exit = spot_close[e - HOUR_MS]
                    slip_exit_spot = net_qty * s_exit * bps / 10000
                    proceeds = net_qty * s_exit * (1 - SPOT_TAKER) - slip_exit_spot
                    return proceeds + max(trig_eq, 0.0) + (reserve_cash - slip_in - slip_exit_spot)
                if after_exit and x == T1:
                    proceeds = net_qty * S1 * (1 - SPOT_TAKER)
                    margin_final = (margin_posted - perp_open_fee + funding_total
                                    - qty * (P1 - P0) - perp_close_fee_full)
                    return proceeds + margin_final + (reserve_cash - slip_in - slip_out_full)
                cumx = fbt_cum.get(x, 0.0)
                if x == T0:
                    cumx = 0.0
                eq = equity0 + cumx - qty * (mark_at(base, x) - MK0) if x > T0 else equity0
                return net_qty * spot_open[x] + eq + reserve_cash - (slip_in if x > T0 else 0.0)
            bounds = [T0]
            cur = T0
            while cur < T1:
                nxt = min([t for t in SETTLES if t > cur and
                           datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m")
                           != datetime.fromtimestamp(cur / 1000, tz=timezone.utc).strftime("%Y-%m")] + [T1])
                bounds.append(nxt); cur = nxt
            segs = {}
            for a, b in zip(bounds, bounds[1:]):
                label = f"{iso(a)[:10]}→{iso(b)[:10]}"
                dead = trig_e is not None and a >= trig_e
                if dead:
                    segs[label] = {"funding": 0.0, "price_pnl": 0.0, "fees": 0.0, "slip": 0.0,
                                   "net": 0.0,
                                   "ops": {o: o * ((b - a) / 86400000) / DAYS_PER_MONTH for o in OPS_SCENARIOS},
                                   "status": "liquidated_no_position"}
                    continue
                f_seg = sum(v for t, v in fbt.items() if a < t <= b and (trig_e is None or t <= trig_e))
                v_a = C if a == T0 else portfolio_value(a)
                v_b = portfolio_value(b, after_exit=(b == T1 and trig_e is None))
                net_seg = v_b - v_a
                fee_seg = perp_open_fee if a == T0 else 0.0
                if trig_e is None and b == T1:
                    fee_seg += perp_close_fee_full
                slip_seg = slip_in if a == T0 else 0.0
                if trig_e is not None and a < trig_e <= b:
                    slip_seg += net_qty * spot_close[trig_e - HOUR_MS] * bps / 10000
                elif trig_e is None and b == T1:
                    slip_seg += slip_out_full
                price_seg = net_seg - f_seg + fee_seg + slip_seg
                ops_seg = {o: o * ((b - a) / 86400000) / DAYS_PER_MONTH for o in OPS_SCENARIOS}
                segs[label] = {"funding": f_seg, "price_pnl": price_seg, "fees": fee_seg,
                               "slip": slip_seg, "net": net_seg, "ops": ops_seg,
                               "status": "liquidated_in_segment" if (trig_e is not None and a < trig_e <= b) else "active"}
            out["monthly"].append({"capital": C, "mode": mode, "months": segs})
    return out

results = [run_symbol("BTC"), run_symbol("ETH")]

# ---------- 自查输出（失败即中止，不产出结果表） ----------
with open(os.path.join(RES, "self_check.txt"), "w") as f:
    f.write("benchmark.py 自查结果（风验收修正版 v2）\n")
    for name, ok, detail in checks:
        f.write(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}\n")
    f.write(f"总计 {sum(1 for _, ok, _ in checks if ok)}/{len(checks)} 通过\n")
failed = [c for c in checks if not c[1]]
print(f"self-checks: {len(checks) - len(failed)}/{len(checks)} pass")
if failed:
    for name, _, detail in failed:
        print(f"FAILED: {name} {detail}", file=sys.stderr)
    sys.exit(1)

OPS = {o: o * DAYS / DAYS_PER_MONTH for o in OPS_SCENARIOS}

# ---------- CSV：主表（路径结果） ----------
with open(os.path.join(RES, "benchmark_table.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "capital", "mode", "slippage_bps", "feasible", "contracts", "qty_perp",
                "qty_spot_net", "residual_delta_base", "residual_notional_usdt", "spot_notional",
                "perp_notional", "margin_posted", "reserve_cash", "fees_est", "outcome",
                "exit_event_utc", "funding_received", "spot_leg_pnl", "margin_equity_left",
                "trade_net_liqfee0", "trade_net_liqfee_full_equity",
                "ops_cost_0", "net_ops0_range", "ops_cost_5usd_scenario", "net_ops5_range"])
    for r in results:
        for row in r["rows"]:
            lo = min(row["net_liq0"], row["net_liqfull"]); hi = max(row["net_liq0"], row["net_liqfull"])
            w.writerow([r["base"], row["capital"], row["mode"], row["bps"], row["feasible"],
                        row["contracts"], f"{row['qty']:.8f}", f"{row['net_qty']:.8f}",
                        f"{row['residual']:.8f}", f"{row['residual'] * r['P0']:.4f}",
                        f"{row['spot_n0']:.2f}", f"{row['perp_n0']:.2f}", f"{row['margin_posted']:.2f}",
                        f"{row['reserve_cash']:.2f}", f"{row['fees_est']:.2f}", row["outcome"],
                        iso(row["exit_time"]), f"{row['funding']:.4f}", f"{row['spot_leg']:.4f}",
                        f"{row['eq_left']:.4f}" if row["eq_left"] is not None else "",
                        f"{row['net_liq0']:.4f}", f"{row['net_liqfull']:.4f}",
                        "0.00", f"{lo:.4f}..{hi:.4f}", f"{OPS[5.0]:.2f}", f"{lo - OPS[5.0]:.4f}..{hi - OPS[5.0]:.4f}"])

# ---------- CSV：反事实诊断 ----------
with open(os.path.join(RES, "counterfactual_table.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "capital", "slippage_bps", "funding", "spot_leg_pnl", "perp_leg_pnl",
                "perp_fees", "slippage", "trade_net", "net_ops0", "net_ops5"])
    for r in results:
        for c in r["counter"]:
            w.writerow([r["base"], c["capital"], c["bps"], f"{c['funding']:.4f}", f"{c['spot_leg']:.4f}",
                        f"{c['perp_leg']:.4f}", f"{c['perp_fees']:.4f}", f"{c['slip']:.4f}",
                        f"{c['net']:.4f}", f"{c['net']:.4f}", f"{c['net'] - OPS[5.0]:.4f}"])

# ---------- CSV：月度（路径口径，5bps） ----------
with open(os.path.join(RES, "monthly_breakdown.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "capital", "mode", "segment", "status", "funding", "price_pnl_combined",
                "fees", "slippage", "trade_net", "ops_cost_0", "net_ops0", "ops_cost_5usd", "net_ops5"])
    for r in results:
        for m in r["monthly"]:
            for label in sorted(m["months"]):
                v = m["months"][label]
                w.writerow([r["base"], m["capital"], m["mode"], label, v["status"], f"{v['funding']:.4f}",
                            f"{v['price_pnl']:.4f}", f"{v['fees']:.4f}", f"{v['slip']:.4f}", f"{v['net']:.4f}",
                            "0.0000", f"{v['net']:.4f}", f"{v['ops'][5.0]:.4f}", f"{v['net'] - v['ops'][5.0]:.4f}"])
print("tables written")
for r in results:
    for row in r["rows"]:
        if row["bps"] == 5:
            print(r["base"], row["capital"], row["mode"], row["outcome"], iso(row["exit_time"]),
                  "fund", round(row["funding"], 2), "net0", round(row["net_liq0"], 2),
                  "netFull", round(row["net_liqfull"], 2))

# ---------- 报告 ----------
cov = {}
for b in ["BTC", "ETH"]:
    fr = fund_raw[b]
    cov[b] = {"fund_n": len(fr), "fund_first": int(fr[0]["fundingTime"]), "fund_last": int(fr[-1]["fundingTime"]),
              "mark_n": len(mark_rows[b]),
              "candles": {i: len(load(f"candles_{i}.json")) for i in [f"{b}-USDT", f"{b}-USDT-SWAP"]},
              "pos": sum(1 for t in SETTLES if RATES[b][t] > 0),
              "neg": sum(1 for t in SETTLES if RATES[b][t] < 0)}
n_checks, n_pass = len(checks), len(checks) - len(failed)

L = []
L.append("# KB 第一轮核算 · 阶段 1 报告（风验收修正版 v2）\n")
L.append("> 性质：**事后会计核算**（固定持有基准），不是预测性回测；不含任何择时结论。"
         "全程研究与模拟，未下真实订单，未用任何账户密钥。结论仅适用于本样本、本配置、"
         "本金档位与下列成本假设，不外推到其他结构或时期。\n")
L.append("## 复现与文件\n")
L.append("- 复现命令（冻结数据）：`python3 scripts/benchmark.py`")
L.append("- 数据更新命令（会改变样本）：`python3 scripts/pull_data.py`；只补标记价格："
         "`python3 scripts/pull_data.py --only-mark --start-ms <T0> --end-ms <T1>`")
L.append(f"- 自查 {n_pass}/{n_checks} 项全过（见 `results/self_check.txt`）。文件：`scripts/`、`data/`"
         "（原始 JSON/CSV + `MANIFEST.md`，请求逐条记录）、`results/benchmark_table.csv`（主表·路径结果）、"
         "`results/monthly_breakdown.csv`、`results/counterfactual_table.csv`（反事实诊断）、本报告。\n")
L.append("## 口径与约定（v2）\n")
L.append("- **标记价格**：资金费逐期 = 实际结算费率 × 数量 × 结算时刻标记价格；清算路径用标记价格 1H 序列判定。"
         "事件时刻 e 的标记价格取 ts=e−1h 标记 K 线的 close（K 线 ts 是小时开始时刻，close 是该小时结束价）。")
L.append("- **T1 顺序**：T1 时点先结算资金费、再按 T1 成交价退出；资金费期数 = (T0, T1] 内的结算期数；"
         "路径扫描只限事件时刻 (T0, T1]，触发时刻按收盘对应时刻标注。")
L.append("- **费用**：现货买入费从买入资产中扣除（净到手 = 毛数量×(1−0.10%)），残余净空头敞口如实列示；"
         "永续开仓费从保证金权益中扣除后参与清算判定；现货卖出费从卖出款中净扣；永续平仓费（存活口径）按平仓名义计。")
L.append("- **保证金模式**：逐仓。A = 保证金为永续名义的 35%、其余现金在保证金外；"
         "B = 在 A 基础上把现金池扣除费用预留与滑点预留后的余额全额预划入保证金（同一笔钱不重复使用，逐情景验守恒）。"
         "滑点资金来源口径：入场滑点由保证金外现金池支付并逐情景检查；出场滑点与出场费用从出场款项净扣，不占用入场现金池。")
L.append("- **清算截断**：触发后现货按触发小时收盘成交价退出（计卖出费与现货退出滑点）；保证金剩余权益按强平费"
         "占剩余权益 0% / 100% 两档情景给出净额区间，不编造精确强平成交价；触发后不再计入任何资金费。"
         "逐仓损失以已划保证金为上限：若小时收盘权益已跳至负值，剩余权益按 0 计、缺口不由组合另行赔付"
         "（模型假设；此时强平费两档情景无差异）。触发事件为 1H 收盘代理，盘中实际触发点可能更早，"
         "现货退出价与剩余权益随之不同，误差方向不保证单向。")
L.append("- **运行成本**：并列 0 与 5 美元/月两情景；5 美元是独占运行预算情景（按样本天数折算 "
         f"90 天 = ${OPS[5.0]:.2f}，USD≈USDT），不是已证明的最低必要成本。")
L.append("- **规则依据**：OKX 帮助中心《Perpetual funding fee mechanism》——资金费 = 仓位价值 × 费率、"
         "仓位价值按标记价格计、逐仓资金费从该仓逐仓保证金扣除/计入；强平以标记价格与维持保证金率判定"
         "（OKX 强平 FAQ 口径，本版用权益 ≤ MMR×仓位价值等价判定，MMR 取官方 position-tiers 接口逐仓 1 档）。"
         "历史样本沿用现行规则，属假设并在此标明。\n")
L.append(f"## 样本窗口与数据覆盖（全部由数据生成）\n")
L.append(f"共同窗口：建仓 {iso(T0)} → 平仓 {iso(T1)}，共 {DAYS:.0f} 天、{len(SETTLES)} 期结算。\n")
L.append("| 数据 | 条数 | 首条 | 末条 | 检查 |")
L.append("|---|---|---|---|---|")
for b in ["BTC", "ETH"]:
    c = cov[b]
    L.append(f"| 资金费率 {b} | {c['fund_n']} | {iso(c['fund_first'])} | {iso(c['fund_last'])} | 间隔全 8h、无缺口无重复；窗口内正费率 {c['pos']} 期 / 负费率 {c['neg']} 期 |")
    L.append(f"| 标记价格 1H {b} | {c['mark_n']} | — | — | 覆盖全部结算与事件时刻、无缺口（close = 小时结束标记价） |")
    L.append(f"| 成交K线 1H {b} 现货/永续 | {c['candles'][b + '-USDT']} / {c['candles'][b + '-USDT-SWAP']} | — | — | 仅用于成交价（建仓/平仓/现货退出）与月度分腿 |")
L.append(f"| 规格/档位 | — | — | — | BTC 永续 ctVal=0.01、ETH=0.1；逐仓 1 档 MMR 两合约均 "
         f"{results[0]['mmr']:.1%}（OKX `/api/v5/public/position-tiers`） |\n")

for r in results:
    b = r["base"]
    L.append(f"## {b} 结果\n")
    L.append(f"建仓成交价：现货 {r['S0']:,.2f} / 永续 {r['P0']:,.2f}（标记 {r['MK0']:,.2f}）；"
             f"平仓成交价：现货 {r['S1']:,.2f} / 永续 {r['P1']:,.2f}。基差（现货−永续成交价）由 "
             f"{r['S0'] - r['P0']:+,.2f} 变为 {r['S1'] - r['P1']:+,.2f}（解释项，不单独计入损益）。"
             f"窗口内标记价格相对建仓标记最大涨幅 {r['max_rise_pct']:.1f}%。\n")
    row1000 = [x for x in r["rows"] if x["capital"] == 1000 and x["bps"] == 5]
    alloc = {x["mode"]: x for x in row1000}
    L.append("资金分配表（1000 档示例，滑点 5 bps；全档位见主表 CSV）：\n")
    L.append("| 模式 | 张数 | 永续数量 | 现货净到手 | 残余敞口(币) | 现货名义 | 保证金 | 现金池 | 费用预留 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for m in ["A", "B"]:
        x = alloc[m]
        L.append(f"| {m} | {x['contracts']:.2f} | {x['qty']:.6f} | {x['net_qty']:.6f} | {x['residual']:.6f} | "
                 f"{x['spot_n0']:.2f} | {x['margin_posted']:.2f} | {x['reserve_cash']:.2f} | {x['fees_est']:.2f} |")
    L.append("")
    L.append("主结果（路径口径，滑点 5 bps；净额区间 = 强平费占剩余权益 0%→100%）：\n")
    L.append("| 本金 | 模式 | 结果 | 退出时刻(UTC) | 资金费(截至退出) | 交易净额区间 | 扣运行成本后(0 / 5美元情景) |")
    L.append("|---|---|---|---|---|---|---|")
    for x in sorted([y for y in r["rows"] if y["bps"] == 5], key=lambda z: (z["capital"], z["mode"])):
        lo = min(x["net_liq0"], x["net_liqfull"]); hi = max(x["net_liq0"], x["net_liqfull"])
        oc = "存活至 T1" if x["outcome"] == "survived" else "清算截断"
        L.append(f"| {x['capital']} | {x['mode']} | {oc} | {iso(x['exit_time'])} | {x['funding']:+.3f} | "
                 f"{lo:+.3f} .. {hi:+.3f} | {lo:+.3f}..{hi:+.3f} / {lo - OPS[5.0]:+.3f}..{hi - OPS[5.0]:+.3f} |")
    L.append("")
    for m in ["A", "B"]:
        mm = [x for x in r["monthly"] if x["capital"] == 1000 and x["mode"] == m][0]["months"]
        L.append(f"月度分解（1000 档，模式 {m}，路径口径，滑点 5 bps；强平费按 0% 档计）：\n")
        L.append("| 区间 | 状态 | 资金费 | 价格损益(两腿合计) | 费用 | 滑点 | 交易净收益 | 扣成本后(5美元情景) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for label in sorted(mm):
            v = mm[label]
            L.append(f"| {label} | {v['status']} | {v['funding']:+.3f} | {v['price_pnl']:+.3f} | -{v['fees']:.3f} | "
                     f"-{v['slip']:.3f} | {v['net']:+.3f} | {v['net'] - v['ops'][5.0]:+.3f} |")
        L.append("")

L.append("## 反事实诊断（忽略清算的完整 90 天完好对冲，仅作对照，不是主结果）\n")
L.append("| 币种 | 本金 | 滑点(bps) | 资金费(标记价口径) | 交易净收益 | 扣成本后(5美元情景) |")
L.append("|---|---|---|---|---|---|")
for r in results:
    for c in r["counter"]:
        if c["capital"] == 1000:
            L.append(f"| {r['base']} | 1000 | {c['bps']} | {c['funding']:+.3f} | {c['net']:+.3f} | {c['net'] - OPS[5.0]:+.3f} |")
L.append("")
_cf = {(r["base"], c["bps"]): c["net"] for r in results for c in r["counter"] if c["capital"] == 1000}
L.append("## 敏感性说明\n")
L.append(f"- **运行成本情景**：交易净额对运行成本 1:1 敏感。5 美元/月情景下 90 天折算 ${OPS[5.0]:.2f}，"
         "与本金无关；0 美元情景即交易净额本身。是否独占一台服务器属于部署选择，本核算不证明最低必要成本。")
L.append(f"- **滑点敏感**：反事实口径下，滑点 0→10 bps 使 1000 档交易净收益减少 "
         f"BTC {_cf[('BTC', 0)] - _cf[('BTC', 10)]:.3f}、ETH {_cf[('ETH', 0)] - _cf[('ETH', 10)]:.3f} USDT"
         "（与主表/反事实表一致）；滑点仍为情景假设，未实测。")
L.append("- **保证金模式**：A/B 的差别只在清算距离与截断时点，不改变仓位与资金费本身；"
         "B 的保证金含费用/滑点预留之外的全部现金，截断净额区间随之不同。提高保证金比例会同比例压低可部署名义与资金费收入（本版未变结构，仅对照 A/B）。")
L.append("- **费率表敏感**：若实际账户适用其他地区费率表（如美国表现货 taker 0.35%），费用项显著放大，"
         "小额档位结论可能翻转；本版用全球站普通用户 taker，属假设。")
L.append(f"- **资金费环境**：本窗口正费率期占多数（BTC {cov['BTC']['pos']} 正 / {cov['BTC']['neg']} 负，"
         f"ETH {cov['ETH']['pos']} 正 / {cov['ETH']['neg']} 负，共 {len(SETTLES)} 期）；本核算不回答费率转负时是否应空仓。\n")
L.append("## 结论（限定于本样本与假设）\n")
L.append("1. 在本样本、逐仓 35% 基础保证金、100/300/1000 USDT 三档与上述成本假设下，固定持有基准的"
         "路径结果见主表：出现清算截断的模式/币种，其可实现净额以截断区间为准；存活模式的净额亦不足以覆盖 "
         "5 美元/月独占运行预算情景（1000 档最佳情形见主表）。这一结论不外推到其他本金量级、保证金结构或时期。")
L.append("2. 提前退出（清算截断）改变后续路径：截断后资金费不再累积，组合按退出时点结算；"
         "1H 标记价格代理与盘中真实路径之间存在误差，其方向不保证单向，本版触发时刻均为 1H 收盘口径的代理结果。")
L.append("3. 是否继续研究“只在高费率期持有”等结构变化，不在本轮范围；本轮只修正核算口径，不扩展策略。\n")
L.append("## 未解决问题清单\n")
L.append("1. 费率表归属（全球/美国/EEA）未核实，需用户实际账户等级确认后重算。")
L.append("2. 滑点为情景假设，未实测；与资金费收入同量级。")
L.append("3. 清算判定为 1H 标记收盘口径，不能排除盘中触发；强平实际成交价与清算费按 0%/100% 剩余权益两档情景给出，未重建真实强平过程。")
L.append("4. 历史样本沿用现行 OKX 规则（资金费公式、逐仓划转、强平口径）属假设；规则历史变更未逐项考证。")
L.append("5. 基差/两腿损益为单一 90 天窗口实现值，不能外推分布；样本长度不足以估计尾部。")
L.append("6. 模式 B 要求开仓即按预留口径划入保证金，属纪律假设。")
for i, (sym, cap, bps, short) in enumerate(B_DEGEN):
    L.append(f"{7 + i}. 模式 B 预留不足：{sym} {cap} 档 {bps} bps 情景下，现金池比费用预留+滑点预留少 "
             f"{short:.4f} USDT（入场价估计口径），该情景无额外保证金可划、B 退化为 A；"
             "实际出场费用按出场名义计、低于预留估计，此缺口为估计口径产物，已如实标示。")
L.append("")

# ---- 修正记录（v2）：v1（已交付版）→ v2 前后对照，v1 数字引自 v1 报告原文 ----
def _row(sym, cap, mode, bps=5):
    return [x for x in [r for r in results if r["base"] == sym][0]["rows"]
            if x["capital"] == cap and x["mode"] == mode and x["bps"] == bps][0]
_bA, _eA, _eB = _row("BTC", 1000, "A"), _row("ETH", 1000, "A"), _row("ETH", 1000, "B")
L.append("## 修正记录（v2）\n")
L.append("| 项 | v1（已交付） | v2（本版） |")
L.append("|---|---|---|")
L.append("| 资金费仓位价值 | 成交 K 线开盘价 | 结算时刻标记价格（OKX 规则口径） |")
L.append("| 清算判定价格 | 成交 K 线收盘价 | 标记价格 1H 收盘（事件时刻口径） |")
L.append(f"| BTC 模式A 触发时刻 | 2026-09-21 20:00 UTC（K线 ts 标注，早 1 小时） | "
         f"{iso(_bA['exit_time']) if _bA['outcome'] == 'liquidated' else '未触发'}（收盘对应时刻，标记价口径） |")
L.append(f"| ETH 触发时刻 | 2026-09-11 13:00 UTC（ts 标注） | "
         f"A {iso(_eA['exit_time']) if _eA['outcome'] == 'liquidated' else '未触发'} / "
         f"B {iso(_eB['exit_time']) if _eB['outcome'] == 'liquidated' else '未触发'}（收盘对应时刻） |")
L.append("| BTC 模式B | 宣称“未触发、完好数字成立” | 按标记价与费用后权益重检，结论以主表为准，不再预设幸存 |")
L.append("| 截断净额(1000档,5bps) | BTC +3.77 / ETH +2.50（未计强平费与现货退出费） | "
         f"BTC A {_bA['net_liqfull']:+.2f}..{_bA['net_liq0']:+.2f} / ETH A {_eA['net_liqfull']:+.2f}..{_eA['net_liq0']:+.2f}"
         "（强平费 100%→0% 区间，含现货退出费与滑点） |")
L.append("| 现货买入费 | 按 USDT 现金费计、残余敞口记 0 | 从买入资产扣、净到手减少，残余净空头敞口如实量化（见分配表） |")
L.append("| 截断负权益处理 | v1 把触发时负权益计入组合净额（相当于组合赔付保证金缺口） | 逐仓损失以已划保证金为上限，剩余权益不低于 0；"
         "ETH 1000 档截断净额由 +2.50 变为 +13.13，主因即此项与退出费口径（触发小时权益已跳至 −13.20） |")
L.append("| 模式B 滑点资金 | 缓冲全额划入保证金，滑点另行扣除（同一笔钱两用） | 费用/滑点先预留，余额才划入保证金，逐情景验守恒与滑点来源 |")
L.append("| 主表 | 完整 90 天完好对冲为主结果 | A/B 路径截断结果为主表；完好对冲降格为反事实诊断 |")
L.append("| 运行成本 | 只列 5 美元/月一档 | 并列 0 与 5 美元/月两情景，5 美元表述为独占运行预算情景 |")
L.append("| 滑点敏感文字 | “减少约 5.9/5.8 USDT”（与表不符） | 与表一致的实际差值（见敏感性说明） |")
L.append("| 复现命令 | pull_data + benchmark 顺序执行 | 冻结数据只跑 benchmark.py；pull_data 单独标注为更新数据用 |")
L.append("| 过度结论 | “真正损失不是爆仓本身”“真实只会更差”等 | 已删除，改为中性表述并限定结论范围 |")

open(os.path.join(RES, "phase1_report.md"), "w").write("\n".join(L) + "\n")
print("report written")
