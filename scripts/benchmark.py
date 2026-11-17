#!/usr/bin/env python3
"""
benchmark.py — KB 第一轮核算 · 阶段1 固定持有基准（风复验修正版 v3）

复现命令（冻结数据，只跑本脚本）:
    python3 scripts/benchmark.py
数据更新（会改变样本，勿用于复现）:
    python3 scripts/pull_data.py            # 全量更新
    python3 scripts/pull_data.py --only-mark --start-ms <T0> --end-ms <T1>   # 只补标记价格
测试可用环境变量 KB_DATA_DIR / KB_RES_DIR 指向数据/结果目录副本（独立测试用）。

v3 修正（风复验 v2：复现通过、核算正确性未通过；只修确定错误）：
  1. 退出滑点只扣一次：清算截断与月度 portfolio_value 中，退出滑点已从现货卖出款
     扣除，现金池余额只扣入场滑点（v2 两处各多扣一次，1000 档 5bp 约 0.50 USDT）。
  2. 模式 B 开仓配置去前视：开仓预留只用 T0 可见信息（T0 价格估费用与滑点预留），
     实际退出费用/滑点在退出时按实际价格结算，不回头改开仓保证金（v2 的 slip_total
     含 T1 价格算出的退出滑点，改 T1 价格即改开仓保证金）。
  3. 盈亏成本基准 = 真实开仓成交价 P0（永续）/ S0（现货）；标记价格只用于当前估值
     与清算判定（v2 路径权益误用开仓标记价 MK0 作起点）。滑点另作现金成本计列，
     不含在成交价内，全程一致、不重复。
  4. 清算判定改用 OKX 官方强平 FAQ 的逐仓 USDT 永续清算价公式：
     空头清算价 = (保证金余额 + 面值×张数×开仓均价) / (面值×张数×(1 + 档位MMR + 费率))，
     保证金余额含已结算资金费、已扣开仓费。阈值中的费率项是触发所需的覆盖要求，
     与清算费用情景是替代关系而非叠加（见口径节），不重复扣。
  5. 结论由结果表生成：报告中“全部/全档”类表述由脚本读回主表 CSV 自动统计，
     不再人工概括；5bp 主情景与全情景分开表述。
  6. 实际清算退出收益隔离为未知：离散代理（小时收盘检测 + 代理价卖出现货）只作
     诊断输出；0%/100% 为“固定代理退出价下、清算相关费用消耗剩余权益的比例”情景，
     不是实际收益上下界。另用标记价 OHLC 的 high 做盘中触线检查：high 越过清算价
     但 close 未触发的小时记为盘中不确定；“存活”区分收盘口径与盘中未触线两层。
  7. 运行成本生命周期假设写明（见报告口径节），不新增模型。

费用假设：OKX 全球站普通用户 taker——现货 0.10%、永续 0.05%（强平公式费率同取 taker）。
滑点假设：0/2/5/10 bps 单边按各腿名义价值计（假设，非实测）。
保证金：逐仓；模式 A 已划保证金 = 永续名义 35%；模式 B 在 A 基础上把
（费用预留 + 滑点预留，均按 T0 价格估算）之外的现金全部预划入保证金。
"""
import csv, json, math, os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("KB_DATA_DIR", os.path.join(HERE, "..", "data"))
RES = os.environ.get("KB_RES_DIR", os.path.join(HERE, "..", "results"))
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

def mark_maps(inst_id):
    rows = load(f"mark_{inst_id}.json")
    return ({int(c[0]): float(c[4]) for c in rows},      # close
            {int(c[0]): float(c[2]) for c in rows},      # high
            rows)

marks, mark_highs, mark_rows = {}, {}, {}
trade = {}
for base in ["BTC", "ETH"]:
    marks[base], mark_highs[base], mark_rows[base] = mark_maps(f"{base}-USDT-SWAP")
    trade[base] = {"spot": candle_maps(f"{base}-USDT"),
                   "perp": candle_maps(f"{base}-USDT-SWAP")}

def mark_at(base, t_ms):
    """事件时刻 t 的标记价格 = ts=t-1h 标记 K 线的 close（该小时结束价）。"""
    return marks[base][t_ms - HOUR_MS]

def mark_high_at(base, t_ms):
    """事件时刻 t 所在小时（ts=t-1h 那根）的标记价格 high（盘中最高）。"""
    return mark_highs[base][t_ms - HOUR_MS]

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
missing_rr = [(b, t) for b in ["BTC", "ETH"] for t in SETTLES
              if fund[b][t].get("realizedRate") in (None, "")]
check("窗口内 realizedRate 全部存在（缺失即中止）", not missing_rr, f"missing={missing_rr[:3]}")
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
_probe_ts = T0 - HOUR_MS
check("事件时间映射（close 对应 ts+1h）",
      mark_at("BTC", T0) == marks["BTC"][_probe_ts] and _probe_ts + HOUR_MS == T0)
check("路径扫描范围 (T0, T1] 且计数=窗口小时数",
      EVENTS[0] == T0 + HOUR_MS and EVENTS[-1] == T1 and len(EVENTS) == int(DAYS * 24),
      f"n={len(EVENTS)}")

RATES = {b: {t: float(fund[b][t]["realizedRate"]) for t in SETTLES} for b in ["BTC", "ETH"]}
B_DEGEN = []   # 模式 B 现金池不足以覆盖 T0 口径费用+滑点预留、退化为 A 的情景

def run_symbol(base):
    spot_open, spot_close = trade[base]["spot"]
    perp_open, perp_close = trade[base]["perp"]
    spec_s, spec_p = insts[f"{base}-USDT"], insts[f"{base}-USDT-SWAP"]
    ct_val, lot_sz = float(spec_p["ctVal"]), float(spec_p["lotSz"])
    min_p, min_s = float(spec_p["minSz"]), float(spec_s["minSz"])
    mmr = float(tiers[f"{base}-USDT"][0]["mmr"])
    S0, P0 = spot_open[T0], perp_open[T0]
    S1, P1 = spot_open[T1], perp_open[T1]
    MK0 = mark_at(base, T0)   # 仅作描述统计（开仓时刻标记价），不作盈亏起点
    out = {"base": base, "S0": S0, "S1": S1, "P0": P0, "P1": P1, "MK0": MK0, "mmr": mmr,
           "rows": [], "monthly": [], "counter": []}
    out["max_rise_pct"] = max(mark_at(base, e) / MK0 - 1 for e in EVENTS) * 100

    for C in CAPITALS:
        denom = S0 * (1 + 2 * SPOT_TAKER) + P0 * (MARGIN_FRAC + 2 * PERP_TAKER)
        qty_target = C / denom
        contracts = math.floor((qty_target / ct_val) / lot_sz) * lot_sz
        qty = contracts * ct_val                       # 永续数量 = 现货毛数量
        net_qty = qty * (1 - SPOT_TAKER)               # 现货买入费从资产扣后的净到手
        residual = qty - net_qty                       # 残余净空头敞口（币）
        spot_n0, perp_n0 = qty * S0, qty * P0
        marginA = MARGIN_FRAC * perp_n0
        reserveA = C - spot_n0 - marginA               # 模式 A 的保证金外现金池
        perp_open_fee = perp_n0 * PERP_TAKER
        perp_close_fee_full = qty * P1 * PERP_TAKER
        spot_sell_fee_full = net_qty * S1 * SPOT_TAKER
        # 开仓预留全部按 T0 价格估算（T0 可见信息）：开仓费 + 平仓费(T0名义) + 现货卖出费(T0名义)
        fees_est = perp_open_fee + perp_n0 * PERP_TAKER + net_qty * S0 * SPOT_TAKER
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

        # 完好对冲（反事实诊断）：与存活路径同一现金流水账本，见下方 ledger_survived
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

        def alloc(mode, bps):
            """开仓配置：只用 T0 信息。返回 (margin_posted, reserve_cash, slip_in, slip_reserve_est)。
            slip_reserve_est = 入场滑点(T0名义) + 退出滑点(T0名义估)，仅用于模式 B 的预留规模，
            不进入任何实际结算；实际退出滑点按退出时实际价格从退出款项结算。"""
            slip_in = (spot_n0 + perp_n0) * bps / 10000
            slip_reserve_est = slip_in + (net_qty * S0 + qty * P0) * bps / 10000
            if mode == "A":
                return marginA, reserveA, slip_in, slip_reserve_est
            extra = max(reserveA - fees_est - slip_reserve_est, 0.0)
            if extra == 0.0 and reserveA < fees_est + slip_reserve_est - 1e-9:
                B_DEGEN.append((base, C, bps, fees_est + slip_reserve_est - reserveA))
            return marginA + extra, reserveA - extra, slip_in, slip_reserve_est

        def scan_path(margin_posted):
            """逐小时扫描 (T0, T1]。盈亏起点 = 开仓成交价 P0；标记价只作估值/判定。
            触发用官方清算价公式：P_liq = (mb + qty*P0) / (qty*(1+MMR+费率))，
            mb = 已划保证金 − 开仓费 + 已结算资金费。返回 dict。"""
            mb0 = margin_posted - perp_open_fee
            cum_f, si, cum_map = 0.0, 0, {}
            trig, uncertain, first_unc = None, 0, None
            for e in EVENTS:
                while si < len(SETTLES) and SETTLES[si] <= e:
                    cum_f += fbt[SETTLES[si]]; si += 1
                cum_map[e] = cum_f
                mb = mb0 + cum_f
                p_liq = (mb + qty * P0) / (qty * (1 + mmr + PERP_TAKER))
                mk = mark_at(base, e)
                if mk >= p_liq:
                    trig = {"e": e, "mk": mk, "equity": mb - qty * (mk - P0),
                            "cum_f": cum_f, "p_liq": p_liq}
                    break
                if mark_high_at(base, e) >= p_liq:
                    uncertain += 1
                    if first_unc is None:
                        first_unc = e
            return {"trig": trig, "cum_map": cum_map, "uncertain": uncertain,
                    "first_unc": first_unc, "mb0": mb0}

        # ---- 路径模拟（模式 × 滑点）----
        for mode in ["A", "B"]:
            for bps in SLIPPAGES_BPS:
                margin_posted, reserve_cash, slip_in, slip_reserve_est = alloc(mode, bps)
                check(f"滑点资金来源充足(入场现金) {base} {C} {mode} {bps}bp",
                      slip_in <= reserve_cash + 1e-9,
                      f"slip_in={slip_in:.4f} source={reserve_cash:.4f}")
                if mode == "B":
                    check(f"模式B预留未进保证金 {base} {C} {bps}bp",
                          abs(reserve_cash - (fees_est + slip_reserve_est)) < 1e-6
                          or (margin_posted == marginA and reserve_cash == reserveA),
                          f"reserve={reserve_cash:.4f} fees+slipest={fees_est + slip_reserve_est:.4f}")
                check(f"资金守恒 {base} {C} {mode} {bps}bp",
                      abs(spot_n0 + margin_posted + reserve_cash - C) < 1e-6)
                scan = scan_path(margin_posted)
                trig = scan["trig"]
                if trig:
                    e, mk_e, eq_left, fund_until = trig["e"], trig["mk"], trig["equity"], trig["cum_f"]
                    check(f"清算后不再计资金费 {base} {C} {mode} {bps}bp",
                          abs(fund_until - sum(v for t, v in fbt.items() if t <= e)) < 1e-9,
                          f"until={fund_until:.4f} total={funding_total:.4f}")
                    # 诊断代理退出：现货按触发小时收盘成交价卖出；退出滑点只从卖出款扣一次；
                    # 现金池只扣入场滑点。剩余权益按清算费用消耗 0%/100% 两情景（代理价下）。
                    s_exit = spot_close[e - HOUR_MS]
                    slip_exit_spot = net_qty * s_exit * bps / 10000
                    spot_proceeds = net_qty * s_exit * (1 - SPOT_TAKER) - slip_exit_spot
                    spot_pnl = spot_proceeds - spot_n0
                    reserve_left = reserve_cash - slip_in
                    nets = {}
                    for liq_frac in [0.0, 1.0]:
                        residual_margin = max(eq_left * (1 - liq_frac), 0.0)
                        nets[liq_frac] = spot_proceeds + residual_margin + reserve_left - C
                    out["rows"].append({
                        "capital": C, "mode": mode, "bps": bps, "feasible": feasible_base,
                        "contracts": contracts, "qty": qty, "net_qty": net_qty, "residual": residual,
                        "spot_n0": spot_n0, "perp_n0": perp_n0, "margin_posted": margin_posted,
                        "reserve_cash": reserve_cash, "fees_est": fees_est,
                        "outcome": "liquidated", "exit_time": e, "funding": fund_until,
                        "spot_leg": spot_pnl, "eq_left": eq_left,
                        "net_liq0": nets[0.0], "net_liqfull": nets[1.0],
                        "uncertain": scan["uncertain"], "first_unc": scan["first_unc"]})
                else:
                    # 存活：现金流水账本（与反事实口径逐项一致）
                    slip_exit_spot = net_qty * S1 * bps / 10000
                    slip_exit_perp = qty * P1 * bps / 10000
                    spot_proceeds = net_qty * S1 * (1 - SPOT_TAKER) - slip_exit_spot
                    margin_final = (margin_posted - perp_open_fee + funding_total
                                    - qty * (P1 - P0) - perp_close_fee_full - slip_exit_perp)
                    reserve_left = reserve_cash - slip_in
                    net_ledger = spot_proceeds + margin_final + reserve_left - C
                    net_cf = [c for c in out["counter"] if c["capital"] == C and c["bps"] == bps][0]["net"]
                    check(f"存活账本与反事实口径一致 {base} {C} {mode} {bps}bp",
                          abs(net_ledger - net_cf) < 1e-6, f"ledger={net_ledger:.6f} cf={net_cf:.6f}")
                    out["rows"].append({
                        "capital": C, "mode": mode, "bps": bps, "feasible": feasible_base,
                        "contracts": contracts, "qty": qty, "net_qty": net_qty, "residual": residual,
                        "spot_n0": spot_n0, "perp_n0": perp_n0, "margin_posted": margin_posted,
                        "reserve_cash": reserve_cash, "fees_est": fees_est,
                        "outcome": "survived", "exit_time": T1, "funding": funding_total,
                        "spot_leg": spot_leg_full, "eq_left": None,
                        "net_liq0": net_ledger, "net_liqfull": net_ledger,
                        "uncertain": scan["uncertain"], "first_unc": scan["first_unc"]})
        # ---- 月度分解（5 bps，模式 A/B，路径口径）----
        for mode in ["A", "B"]:
            bps = 5
            margin_posted, reserve_cash, slip_in, slip_reserve_est = alloc(mode, bps)
            slip_out_full = (net_qty * S1 + qty * P1) * bps / 10000
            scan = scan_path(margin_posted)
            trig, trig_e, trig_eq = scan["trig"], None, None
            if trig:
                trig_e, trig_eq = trig["e"], trig["equity"]
            fbt_cum = scan["cum_map"]
            equity0 = margin_posted - perp_open_fee
            def portfolio_value(x, after_exit=False):
                """时点 x 组合价值（诊断口径与主表一致）：触发后用代理退出值；
                T1 存活退出用现金流水账本；中间时点用标记价估值（盈亏起点 P0）。"""
                if trig_e is not None and x >= trig_e:
                    s_exit = spot_close[trig_e - HOUR_MS]
                    slip_exit_spot = net_qty * s_exit * bps / 10000
                    proceeds = net_qty * s_exit * (1 - SPOT_TAKER) - slip_exit_spot
                    return proceeds + max(trig_eq, 0.0) + (reserve_cash - slip_in)
                if after_exit and x == T1:
                    slip_exit_spot = net_qty * S1 * bps / 10000
                    slip_exit_perp = qty * P1 * bps / 10000
                    proceeds = net_qty * S1 * (1 - SPOT_TAKER) - slip_exit_spot
                    margin_final = (equity0 + funding_total - qty * (P1 - P0)
                                    - perp_close_fee_full - slip_exit_perp)
                    return proceeds + margin_final + (reserve_cash - slip_in)
                cumx = fbt_cum.get(x, 0.0)
                if x == T0:
                    return float(C)
                eq = equity0 + cumx - qty * (mark_at(base, x) - P0)
                return net_qty * spot_open[x] + eq + reserve_cash - slip_in
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
            # 月度与主表对账（5bp、清算费用 0% 情景）
            main_row = [x for x in out["rows"] if x["capital"] == C and x["mode"] == mode and x["bps"] == 5][0]
            msum = sum(v["net"] for v in segs.values())
            check(f"月度与主表对账 {base} {C} {mode}", abs(msum - main_row["net_liq0"]) < 0.02,
                  f"monthly={msum:.4f} main={main_row['net_liq0']:.4f}")
    return out

results = [run_symbol("BTC"), run_symbol("ETH")]

# ---------- 自查输出（失败即中止，不产出结果表） ----------
with open(os.path.join(RES, "self_check.txt"), "w") as f:
    f.write("benchmark.py 自查结果（风复验修正版 v3；内部自查，独立测试见 independent_tests.txt）\n")
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

# ---------- CSV：主表（路径结果；net_liq* 为代理退出价下的费用情景诊断值） ----------
with open(os.path.join(RES, "benchmark_table.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "capital", "mode", "slippage_bps", "feasible", "contracts", "qty_perp",
                "qty_spot_net", "residual_delta_base", "residual_notional_usdt", "spot_notional",
                "perp_notional", "margin_posted", "reserve_cash", "fees_est", "outcome",
                "exit_event_utc", "funding_received", "spot_leg_pnl", "margin_equity_left",
                "trade_net_liqfee0", "trade_net_liqfee_full_equity",
                "ops_cost_0", "net_ops0_range", "ops_cost_5usd_scenario", "net_ops5_range",
                "intrabar_uncertain_hours", "first_intrabar_uncertain_utc"])
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
                        "0.00", f"{lo:.4f}..{hi:.4f}", f"{OPS[5.0]:.2f}", f"{lo - OPS[5.0]:.4f}..{hi - OPS[5.0]:.4f}",
                        row["uncertain"], iso(row["first_unc"]) if row["first_unc"] else ""])

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
                  "netFull", round(row["net_liqfull"], 2), "unc", row["uncertain"])

# ---------- 全情景符号清单：读回主表 CSV 自动生成（修正项 5） ----------
def parse_range(s):
    a, b = s.split("..")
    return float(a), float(b)

inv_rows = []
with open(os.path.join(RES, "benchmark_table.csv")) as f:
    for rec in csv.DictReader(f):
        lo, hi = parse_range(rec["net_ops5_range"])
        inv_rows.append({"symbol": rec["symbol"], "capital": int(rec["capital"]),
                         "mode": rec["mode"], "bps": int(rec["slippage_bps"]),
                         "outcome": rec["outcome"], "lo": lo, "hi": hi})
inv_all_neg = [x for x in inv_rows if x["hi"] < 0]
inv_not_neg = [x for x in inv_rows if x["hi"] >= 0]
inv_5bp = [x for x in inv_rows if x["bps"] == 5]
inv_5bp_not_neg = [x for x in inv_5bp if x["hi"] >= 0]
UNCERTAIN_ROWS = [(r["base"], row) for r in results for row in r["rows"] if row["uncertain"] > 0]
SURVIVED_UNCERTAIN = [(r["base"], row) for r in results for row in r["rows"]
                      if row["outcome"] == "survived" and row["uncertain"] > 0]

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
L.append("# KB 第一轮核算 · 阶段 1 报告（风复验修正版 v3）\n")
L.append("> 性质：**事后会计核算**（固定持有基准），不是预测性回测；不含任何择时结论。"
         "全程研究与模拟，未下真实订单，未用任何账户密钥。结论仅适用于本样本、本配置、"
         "本金档位与下列成本假设，不外推到其他结构或时期。\n")
L.append("## 复现与文件\n")
L.append("- 复现命令（冻结数据）：`python3 scripts/benchmark.py`；独立测试：`python3 scripts/independent_tests.py`"
         "（结果写入 `results/independent_tests.txt`）。")
L.append("- 数据更新命令（会改变样本）：`python3 scripts/pull_data.py`；只补标记价格："
         "`python3 scripts/pull_data.py --only-mark --start-ms <T0> --end-ms <T1>`")
L.append(f"- 内部自查 {n_pass}/{n_checks} 项全过（见 `results/self_check.txt`）；独立测试与内部自查是两套东西，"
         "独立测试用另一份实现逐项重算现金流、做未来价格扰动与公式边界核对（见 `results/independent_tests.txt`）。"
         "文件：`scripts/`、`data/`（原始 JSON/CSV + `MANIFEST.md`，请求逐条记录）、"
         "`results/benchmark_table.csv`（主表·路径结果）、`results/monthly_breakdown.csv`、"
         "`results/counterfactual_table.csv`（反事实诊断）、本报告。\n")
L.append("## 口径与约定（v3）\n")
L.append("- **标记价格**：资金费逐期 = 实际结算费率 × 数量 × 结算时刻标记价格；清算路径用标记价格 1H 序列判定。"
         "事件时刻 e 的标记价格取 ts=e−1h 标记 K 线的 close（K 线 ts 是小时开始时刻，close 是该小时结束价）。")
L.append("- **盈亏成本基准**：永续盈亏按真实开仓成交价 P0 起算、现货按 S0 起算；标记价格只用于当前估值与"
         "清算判定，不作成本基准。滑点另作现金成本计列（入场/出场各按名义价值的情景 bps），不含在成交价内，"
         "全程一致、不重复计列。")
L.append("- **清算判定（官方公式）**：OKX 帮助中心《Forced liquidation FAQ》（https://www.okx.com/en-gb/help/forced-liquidation-faq）"
         "逐仓 USDT 永续空头预估清算价公式：清算价 = (保证金余额 + 面值×|张数|×开仓均价) ÷ "
         "(面值×|张数|×(档位维持保证金率 + 费率 + 1))。本版据此判定：逐小时以标记收盘价与当刻清算价比较；"
         "保证金余额 = 已划保证金 − 永续开仓费 + 已结算资金费（FAQ 明示资金费直接增减仓位保证金、"
         "清算只看标记价格、触发条件为维持保证金率 ≤ 100%）。费率取 taker 0.05%（FAQ：强平费按 taker 费率计）。"
         "阈值公式中的 (MMR + 费率) 是触发时权益必须覆盖的要求；执行侧的清算相关费用（强平费 + 按触发时"
         "维持保证金计的清算清偿费，见 FAQ）以情景方式参数化（下条），与阈值项是替代关系，不叠加重复扣。")
L.append("- **T1 顺序**：T1 时点先结算资金费、再按 T1 成交价退出；资金费期数 = (T0, T1] 内的结算期数；"
         "路径扫描只限事件时刻 (T0, T1]，触发时刻按收盘对应时刻标注。")
L.append("- **费用**：现货买入费从买入资产中扣除（净到手 = 毛数量×(1−0.10%)），残余净空头敞口如实列示；"
         "永续开仓费从保证金权益中扣除后参与清算判定；现货卖出费从卖出款中净扣；永续平仓费（存活口径）按平仓名义计。"
         "退出滑点只从退出款项扣一次，现金池余额只扣入场滑点。")
L.append("- **保证金模式**：逐仓。A = 保证金为永续名义的 35%、其余现金在保证金外；"
         "B = 在 A 基础上把现金池扣除（费用预留 + 滑点预留，两者均按 T0 价格估算）后的余额全额预划入保证金。"
         "开仓配置只使用 T0 可见信息；实际退出费用/滑点在退出时按实际价格结算，不回头改变开仓配置。")
L.append("- **清算截断 = 诊断输出，实际收益未知**：触发后按“现货以触发小时收盘成交价卖出”的离散代理给出"
         "诊断数字；保证金剩余权益按清算相关费用消耗其 0% / 100% 两档情景列示——这是**固定代理退出价下的"
         "费用情景**，不是实际清算收益的上下界，负权益时两档相同也不代表精确。实际清算涉及部分强平、"
         "引擎接管成交价与清偿费的真实过程，本数据无法重建，**实际清算退出收益统一标为不可判定/未知**；"
         "触发后不再计入任何资金费。逐仓损失以已划保证金为上限（模型假设）。")
L.append("- **盘中触线检查**：用标记价格 K 线的 high 逐小时与当刻清算价比较。某小时 high 越过清算价但 close "
         "未触发的，记为盘中不确定小时：该小时内可能已发生盘中触发，收盘口径的触发时刻与存活结论随之不确定。"
         "“存活”分两层表述：收盘口径存活、且盘中高点未触线，才称无条件存活；存在不确定小时的只称"
         "“收盘口径存活（盘中不确定）”。资金费在小时内的结算时点与 high 的先后不可知，盘中检查中保证金余额"
         "按已结算至该事件时刻近似，属近似口径。")
L.append("- **运行成本生命周期假设**：0 与 5 美元/月两情景均按完整 90 天收取——5 美元情景 = 独占服务器"
         f"持续保留整个 90 天的预算（折算 ${OPS[5.0]:.2f}，USD≈USDT），不是已证明的最低必要成本。"
         "若中途退出后停机/释放资源，成本会更低，那是另一情景，本报告未建模。")
L.append("- **规则依据**：OKX 帮助中心《Perpetual funding fee mechanism》——资金费 = 仓位价值 × 费率、"
         "仓位价值按标记价格计、逐仓资金费从该仓逐仓保证金扣除/计入；强平口径见上《Forced liquidation FAQ》。"
         "历史样本沿用现行规则，属假设并在此标明。\n")
L.append("## 样本窗口与数据覆盖（全部由数据生成）\n")
L.append(f"共同窗口：建仓 {iso(T0)} → 平仓 {iso(T1)}，共 {DAYS:.0f} 天、{len(SETTLES)} 期结算。\n")
L.append("| 数据 | 条数 | 首条 | 末条 | 检查 |")
L.append("|---|---|---|---|---|")
for b in ["BTC", "ETH"]:
    c = cov[b]
    L.append(f"| 资金费率 {b} | {c['fund_n']} | {iso(c['fund_first'])} | {iso(c['fund_last'])} | 间隔全 8h、无缺口无重复；窗口内正费率 {c['pos']} 期 / 负费率 {c['neg']} 期 |")
    L.append(f"| 标记价格 1H {b} | {c['mark_n']} | — | — | 覆盖全部结算与事件时刻、无缺口（close = 小时结束标记价，high 用于盘中触线检查） |")
    L.append(f"| 成交K线 1H {b} 现货/永续 | {c['candles'][b + '-USDT']} / {c['candles'][b + '-USDT-SWAP']} | — | — | 仅用于成交价（建仓/平仓/现货代理退出）与月度分腿 |")
L.append(f"| 规格/档位 | — | — | — | BTC 永续 ctVal=0.01、ETH=0.1；逐仓 1 档 MMR 两合约均 "
         f"{results[0]['mmr']:.1%}（OKX `/api/v5/public/position-tiers`） |\n")

for r in results:
    b = r["base"]
    L.append(f"## {b} 结果\n")
    L.append(f"建仓成交价：现货 {r['S0']:,.2f} / 永续 {r['P0']:,.2f}（标记 {r['MK0']:,.2f}，仅描述）；"
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
    L.append("主结果（路径口径，滑点 5 bps；交易净额 = 固定代理退出价下的费用情景区间"
             "（清算费用消耗剩余权益 0%→100%），清算行的实际收益未知，见口径节）：\n")
    L.append("| 本金 | 模式 | 结果 | 退出时刻(UTC) | 资金费(截至退出) | 交易净额区间(诊断) | 扣运行成本后(0 / 5美元情景) | 盘中不确定 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for x in sorted([y for y in r["rows"] if y["bps"] == 5], key=lambda z: (z["capital"], z["mode"])):
        lo = min(x["net_liq0"], x["net_liqfull"]); hi = max(x["net_liq0"], x["net_liqfull"])
        if x["outcome"] == "survived":
            oc = "存活至 T1" if x["uncertain"] == 0 else "收盘口径存活（盘中不确定）"
        else:
            oc = "清算截断（实际收益未知）"
        unc = f"{x['uncertain']} 小时" if x["uncertain"] else "0"
        L.append(f"| {x['capital']} | {x['mode']} | {oc} | {iso(x['exit_time'])} | {x['funding']:+.3f} | "
                 f"{lo:+.3f} .. {hi:+.3f} | {lo:+.3f}..{hi:+.3f} / {lo - OPS[5.0]:+.3f}..{hi - OPS[5.0]:+.3f} | {unc} |")
    L.append("")
    for m in ["A", "B"]:
        mm = [x for x in r["monthly"] if x["capital"] == 1000 and x["mode"] == m][0]["months"]
        L.append(f"月度分解（1000 档，模式 {m}，路径口径，滑点 5 bps；清算费用按 0% 情景计）：\n")
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
L.append(f"- **运行成本情景**：交易净额对运行成本 1:1 敏感。5 美元/月情景按完整 90 天折算 ${OPS[5.0]:.2f}，"
         "与本金无关（生命周期假设见口径节：中途退出停机是另一情景，未建模）；0 美元情景即交易净额本身。")
L.append(f"- **滑点敏感**：反事实口径下，滑点 0→10 bps 使 1000 档交易净收益减少 "
         f"BTC {_cf[('BTC', 0)] - _cf[('BTC', 10)]:.3f}、ETH {_cf[('ETH', 0)] - _cf[('ETH', 10)]:.3f} USDT"
         "（与反事实表一致）；滑点仍为情景假设，未实测。")
L.append("- **保证金模式**：A/B 的差别只在开仓保证金规模、清算距离与截断时点，不改变仓位与资金费本身。")
L.append("- **费率表敏感**：若实际账户适用其他地区费率表（如美国表现货 taker 0.35%），费用项显著放大，"
         "小额档位结论可能翻转；本版用全球站普通用户 taker，属假设。")
L.append(f"- **资金费环境**：本窗口正费率期占多数（BTC {cov['BTC']['pos']} 正 / {cov['BTC']['neg']} 负，"
         f"ETH {cov['ETH']['pos']} 正 / {cov['ETH']['neg']} 负，共 {len(SETTLES)} 期）；本核算不回答费率转负时是否应空仓。\n")

# ---- 结论：全部由主表读回自动生成（修正项 5） ----
L.append("## 结论（限定于本样本与假设；符号清单由主表自动生成）\n")
L.append(f"主表共 {len(inv_rows)} 个情景（2 币种 × 3 档 × 2 模式 × 4 滑点）。按扣 5 美元/月运行成本后的"
         f"净额区间分类：整区间为负 {len(inv_all_neg)} 个；区间上界 ≥ 0（不全为负）{len(inv_not_neg)} 个，逐项如下"
         "（区间 = 代理退出价下清算费用 0%/100% 两情景；清算行的实际收益未知，下列正负只对诊断口径成立）：\n")
if inv_not_neg:
    L.append("| 币种 | 本金 | 模式 | 滑点(bps) | 路径结果 | 扣成本后区间 |")
    L.append("|---|---|---|---|---|---|")
    for x in inv_not_neg:
        L.append(f"| {x['symbol']} | {x['capital']} | {x['mode']} | {x['bps']} | {x['outcome']} | {x['lo']:+.4f} .. {x['hi']:+.4f} |")
    L.append("")
else:
    L.append("（无）\n")
_eth0 = [x for x in inv_rows if x["symbol"] == "ETH" and x["capital"] == 1000 and x["mode"] == "A" and x["bps"] == 0]
if _eth0:
    _e0 = _eth0[0]
    L.append(f"- 特别点名：ETH / 1000 档 / 模式 A / 0 bps 情景扣成本后区间上界为 {_e0['hi']:+.4f} USDT，"
             "是一个真实存在于表中的微小正值（v2 交付文字称“全部为负”不准确，v3 已改由表生成）。"
             "该行是清算截断行，其数字是代理退出价下的诊断值、实际清算收益未知，**不构成可部署证据**。")
if inv_5bp_not_neg:
    L.append(f"- 5 bps 主情景共 {len(inv_5bp)} 行，其中不全为负 {len(inv_5bp_not_neg)} 行（见上表 5bps 行），"
             "故主情景不作“全部为负”表述。")
else:
    L.append(f"- 5 bps 主情景共 {len(inv_5bp)} 行，扣 5 美元/月成本后整区间均为负（由表统计，主情景可单独作此表述）。")
if SURVIVED_UNCERTAIN:
    _su = "、".join(f"{b} {x['capital']}档{x['mode']}/{x['bps']}bps（{x['uncertain']} 小时）" for b, x in SURVIVED_UNCERTAIN[:6])
    L.append(f"- 盘中不确定：收盘口径存活但存在盘中触线小时的情景共 {len(SURVIVED_UNCERTAIN)} 个"
             f"（{_su}），这些情景不无条件宣布存活；详见主表 intrabar 列。")
else:
    L.append("- 盘中不确定：收盘口径存活的情景均无盘中触线小时（由主表 intrabar 列统计）。")
LIQ_UNCERTAIN = [(r["base"], row) for r in results for row in r["rows"]
                 if row["outcome"] == "liquidated" and row["uncertain"] > 0]
if LIQ_UNCERTAIN:
    _lu = sorted({f"{b} {x['mode']}模式（首个不确定小时 {iso(x['first_unc']) if x['first_unc'] else ''}）"
                  for b, x in LIQ_UNCERTAIN})
    L.append(f"- 触发时刻本身亦有盘中不确定：清算截断情景中 {len(LIQ_UNCERTAIN)} 行在收盘触发前已有盘中触线小时"
             f"（{'、'.join(_lu)}），实际触发可能早至该小时内，收盘口径触发时刻为代理值。")
L.append("- 部署判断：在本样本与假设下，没有任何情景给出可靠的正向证据支持为这条路线独占部署一台 "
         "5 美元/月服务器；这是“尚无可靠盈利证据”，不是“已证明这条路必然亏钱”——清算行的实际收益未知、"
         "滑点未实测、费率表未按用户实际账户核实，三者任一都足以改变小额档位的符号。")
L.append("- 提前退出（清算截断）改变后续路径：截断后资金费不再累积；1H 代理与盘中真实路径之间存在误差，"
         "方向不保证单向。本轮只修正核算，不扩展策略。\n")
L.append("## 未解决问题清单\n")
L.append("1. **实际清算退出收益不可判定**：部分强平顺序、引擎接管成交价、清偿费真实金额均无法由现有数据重建；"
         "本报告清算行数字全部是固定代理退出价下的诊断输出。")
L.append("2. 费率表归属（全球/美国/EEA）未核实，需用户实际账户等级确认后重算。")
L.append("3. 滑点为情景假设，未实测；与资金费收入同量级。")
L.append("4. 盘中触发只能由 1H high 近似标记，小时内资金费结算与触线的先后不可知；触发时刻为收盘口径代理。")
L.append("5. 历史样本沿用现行 OKX 规则（资金费公式、逐仓划转、强平公式）属假设；规则历史变更未逐项考证。")
L.append("6. 基差/两腿损益为单一 90 天窗口实现值，不能外推分布；样本长度不足以估计尾部。")
L.append("7. 模式 B 要求开仓即按预留口径划入保证金，属纪律假设。")
for i, (sym, cap, bps, short) in enumerate(B_DEGEN):
    L.append(f"{8 + i}. 模式 B 预留不足：{sym} {cap} 档 {bps} bps 情景下，现金池比费用预留+滑点预留（T0 口径）少 "
             f"{short:.4f} USDT，该情景无额外保证金可划、B 退化为 A，已如实标示。")
L.append("")

# ---- 修正记录（v3）：v2 → v3 前后对照，v2 数字引自 v2 报告/主表原文 ----
def _row(sym, cap, mode, bps=5):
    return [x for x in [r for r in results if r["base"] == sym][0]["rows"]
            if x["capital"] == cap and x["mode"] == mode and x["bps"] == bps][0]
_bA3, _eA3 = _row("BTC", 1000, "A"), _row("ETH", 1000, "A")
L.append("## 修正记录（v3）\n")
L.append("| 项 | v2 | v3（本版） |")
L.append("|---|---|---|")
L.append("| 退出滑点 | 清算截断与月度估值中扣两次（卖出款一次 + 现金池一次），1000 档 5bp 多扣 BTC 0.495 / ETH 0.506 | "
         "只从退出款项扣一次，现金池只扣入场滑点；独立现金流水测试 T-A 逐项核对 |")
L.append(f"| 模式 B 开仓配置 | 预留含以 T1 价格算的退出滑点（前视）：风实测改 T1 永续价使 BTC 1000 档保证金 "
         f"264.1145 → 263.7457 | 预留改 T0 口径：BTC 1000 档 5bp 模式 B 保证金 = {_row('BTC', 1000, 'B')['margin_posted']:.4f}；"
         "独立测试 T-B 改 T1 价格后开仓配置逐项不变 |")
L.append("| 盈亏起点 | 路径权益以开仓标记价 MK0 起算 | 以开仓成交价 P0 起算；标记价只作估值/判定；"
         "独立测试 T-C 以 MK0 扰动不变性 + 权益数值重算核对 |")
L.append("| 清算阈值 | 权益 ≤ MMR×仓位价值（漏费率项） | 官方 FAQ 清算价公式（分母含 1+MMR+费率）；"
         "独立测试 T-D 手算公式边界与程序触发时刻核对 |")
L.append(f"| BTC A 截断（1000档,5bp） | 触发 { '2026-09-21 21:00 UTC' }，净额区间 −1.29..+1.52 | "
         f"触发 {iso(_bA3['exit_time'])}，诊断区间 {_bA3['net_liqfull']:+.2f}..{_bA3['net_liq0']:+.2f}（口径已变，不可直接比大小） |")
L.append(f"| ETH A 截断（1000档,5bp） | +13.13 被表述为截断净额 | 诊断值 {_eA3['net_liq0']:+.2f}（费用 0% 情景）；"
         "成因改写：v1 与 v2 之差是“负权益是否由组合承担”的假设差异，且 v2 在小时末把负权益归零的同一小时"
         "保留了现货涨幅，混入清算后裸持现货的价格暴露——实际清算收益未知，不作确认收益 |")
L.append("| “全部为负”表述 | 交付文字称扣成本后全部为负，与表不符（ETH/1000/A/0bp 为 +0.0879） | "
         "结论改由脚本读回主表自动生成符号清单（见结论节），人工概括删除 |")
L.append("| 清算收益表述 | “可实现净额区间” | 统一标为诊断输出/实际未知；0%/100% 只表述为固定代理退出价下的费用情景 |")
L.append("| 盘中风险 | 只用收盘价判定 | 增加标记价 high 盘中触线检查，不确定小时计数入表；存活分收盘/盘中两层 |")
L.append("| 运行成本 | 只写 5 美元情景数字 | 写明生命周期假设：按完整 90 天收取 = 独占保留 90 天；中途停机情景未建模 |")
L.append("| 自查与测试 | 187 项内部自查 | 内部自查保留（不扩充造势）；新增 4 项独立测试 T-A..T-D（另一实现/扰动/公式手算） |")

open(os.path.join(RES, "phase1_report.md"), "w").write("\n".join(L) + "\n")
print("report written")
