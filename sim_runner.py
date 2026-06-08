"""
TW-Radar 虛擬投資客每日執行引擎
每日收盤後（14:10）自動跑，18:00 輸出報告

執行：.venv/bin/python sim_runner.py
或由 scheduler.py 每日14:15觸發
"""

import os, sys, json
sys.path.insert(0, os.path.dirname(__file__))
# API Key 從 .streamlit/secrets.toml 或環境變數取得
try:
    import toml
    secrets = toml.load(os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml"))
    os.environ.setdefault("ANTHROPIC_API_KEY", secrets.get("ANTHROPIC_API_KEY", ""))
except Exception:
    pass  # 若已在環境變數中則跳過

from datetime import datetime
from utils.virtual_investors import (
    INVESTORS, get_scan_list, init_sim_db,
    get_sim_capital, get_sim_holdings, sim_buy, sim_sell,
    save_daily_report, record_prediction, verify_predictions, get_accuracy_stats
)
from utils.stock_data import get_stock_price, compute_technical_score, get_institutional, get_chip_score
from utils.fundamental_score import get_fundamental_score
from utils.sector_score import get_sector_score
from utils.prophet_score import get_prophet_stock_score
from utils.attack_detector import detect_attacks


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def compute_full_score(stock_id: str, attack_data: dict, news_list: list = None) -> dict:
    """計算五層評分，回傳分數字典"""
    try:
        df   = get_stock_price(stock_id, days=90)
        tech = compute_technical_score(df)
        chip = get_chip_score(get_institutional(stock_id, days=30))
        fund = get_fundamental_score(stock_id)
        sect = get_sector_score(stock_id, attack_data)
        proph = get_prophet_stock_score(stock_id, news_list, attack_data)

        total = (tech["score"] or 0) + (chip["score"] or 0) + \
                (fund["score"] or 0) + (sect["score"] or 0) + (proph["score"] or 0)
        price = tech.get("current", 0)

        return {
            "total": total, "price": price,
            "tech": tech["score"], "chip": chip["score"],
            "fund": fund["score"], "sect": sect["score"],
            "proph": proph["score"]
        }
    except Exception as e:
        return {"total": 0, "price": 0, "error": str(e)}


def run_daily_simulation():
    today = datetime.today()
    if today.weekday() >= 5:
        log("週末，跳過")
        return

    date_str = today.strftime("%Y-%m-%d")
    log(f"=== {date_str} 虛擬投資客模擬開始 ===")

    init_sim_db()

    # 驗證5天前預測
    verify_predictions()

    # 取主力攻擊資料
    log("取得主力攻擊資料...")
    attack_data = detect_attacks()

    # 動態取得全市場篩選後的掃描清單
    scan_list = get_scan_list(attack_data)
    log(f"掃描 {len(scan_list)} 支股票（全市場動態篩選）...")
    scores_cache = {}
    for sid, sname in scan_list:
        s = compute_full_score(sid, attack_data)
        scores_cache[sid] = {**s, "name": sname}
        if s["total"] >= 40:
            log(f"  {sid} {sname}: {s['total']}/100 @ {s['price']:.0f}")

    # 記錄預測（供後續驗證準確度）
    for sid, data in scores_cache.items():
        if data.get("price", 0) > 0:
            signal = "強烈買進" if data["total"] >= 75 else \
                     "可考慮買進" if data["total"] >= 60 else \
                     "觀察等待" if data["total"] >= 45 else "不建議"
            record_prediction(sid, data["name"], date_str, signal, data["total"], data["price"])

    # 三個投資客各自操作
    all_reports = {}
    for inv_id, inv in INVESTORS.items():
        log(f"\n--- {inv['emoji']} {inv['name']} ({inv['title']}) ---")
        report = run_investor_day(inv, scores_cache, date_str)
        all_reports[inv_id] = report
        save_daily_report(date_str, inv_id, report)

        cap = get_sim_capital(inv_id)
        holdings = get_sim_holdings(inv_id)
        market_val = sum(
            scores_cache.get(r["stock_id"], {}).get("price", r["buy_price"]) * r["shares"]
            for _, r in holdings.iterrows()
        )
        total_assets = cap["cash"] + market_val
        pnl_pct = (total_assets - inv["capital"]) / inv["capital"] * 100
        log(f"  資產: NT${total_assets:,.0f} ({pnl_pct:+.1f}%)")

    # 準確率統計
    acc = get_accuracy_stats()
    log(f"\n系統預測準確率: {acc['accuracy']}% ({acc['correct']}/{acc['total']}筆)")

    # 存摘要
    summary_path = os.path.join(os.path.dirname(__file__), "data", f"sim_report_{date_str}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "reports": all_reports,
            "accuracy": acc
        }, f, ensure_ascii=False, indent=2)

    log(f"=== 完成，報告存至 {summary_path} ===")
    return all_reports


def run_investor_day(inv: dict, scores: dict, date_str: str) -> dict:
    """單一投資客的一天操作"""
    strategy = inv["strategy"]
    holdings = get_sim_holdings(inv["id"])
    cap = get_sim_capital(inv["id"])
    actions = []

    # ── 先檢查現有持股：停損/停利/到期 ──────────────────
    if not holdings.empty:
        for _, h in holdings.iterrows():
            sid = h["stock_id"]
            buy_p = float(h["buy_price"])
            shares = float(h["shares"])
            buy_date = h["buy_date"]
            current_p = scores.get(sid, {}).get("price", buy_p)

            if current_p <= 0:
                continue

            pnl_pct = (current_p - buy_p) / buy_p
            days_held = (datetime.strptime(date_str, "%Y-%m-%d") -
                         datetime.strptime(buy_date, "%Y-%m-%d")).days

            reason = None
            if pnl_pct <= strategy["stop_loss"]:
                reason = f"停損 {pnl_pct*100:.1f}%"
            elif pnl_pct >= strategy["take_profit"]:
                reason = f"停利 {pnl_pct*100:.1f}%"
            elif days_held >= strategy["hold_days"]:
                reason = f"持有{days_held}天到期"

            if reason:
                pnl = sim_sell(inv["id"], int(h["id"]), sid, h["stock_name"],
                               current_p, shares, buy_p, scores.get(sid, {}).get("total", 0),
                               reason, date_str)
                action_txt = f"賣出 {sid}{h['stock_name']} @{current_p:.0f}，{reason}，損益 NT${pnl:+,.0f}"
                actions.append({"type": "SELL", "stock": sid, "name": h["stock_name"],
                                 "price": current_p, "pnl": pnl, "reason": reason})
                log(f"  {inv['emoji']} {action_txt}")

    # ── 再掃描買入機會 ──────────────────────────────────
    holdings = get_sim_holdings(inv["id"])  # 重新讀取
    held_ids = set(holdings["stock_id"].tolist()) if not holdings.empty else set()
    n_positions = len(held_ids)
    cap = get_sim_capital(inv["id"])
    available_cash = cap["cash"]

    candidates = sorted(
        [(sid, d) for sid, d in scores.items() if d["total"] >= strategy["min_score"] and sid not in held_ids],
        key=lambda x: x[1]["total"], reverse=True
    )

    for sid, data in candidates:
        if n_positions >= strategy["max_positions"]:
            break
        if available_cash < 5000:
            break

        price = data.get("price", 0)
        if price <= 0:
            continue

        # 計算買入股數（依position size）
        budget = min(available_cash * strategy["position_size"],
                     available_cash * 0.8)
        shares = int(budget / price)
        if shares < 1:
            continue

        cost = price * shares
        if cost > available_cash:
            shares = int(available_cash * 0.9 / price)
            cost = price * shares

        if shares < 1:
            continue

        reason = f"五層評分{data['total']}/100 超過門檻{strategy['min_score']}"
        sim_buy(inv["id"], sid, data["name"], price, shares, data["total"], reason, date_str)
        available_cash -= cost
        n_positions += 1
        actions.append({
            "type": "BUY", "stock": sid, "name": data["name"],
            "price": price, "shares": shares, "score": data["total"],
            "cost": cost, "reason": reason
        })
        log(f"  {inv['emoji']} 買入 {sid}{data['name']} @{price:.0f} {shares}股，評分{data['total']}")

    # 產出當日報告
    cap_final = get_sim_capital(inv["id"])
    holdings_final = get_sim_holdings(inv["id"])
    market_val = sum(
        scores.get(r["stock_id"], {}).get("price", r["buy_price"]) * r["shares"]
        for _, r in holdings_final.iterrows()
    ) if not holdings_final.empty else 0
    total_assets = cap_final["cash"] + market_val
    pnl_pct = (total_assets - inv["capital"]) / inv["capital"] * 100

    return {
        "date": date_str,
        "investor": inv["name"],
        "title": inv["title"],
        "actions": actions,
        "holdings": holdings_final[["stock_id","stock_name","buy_price","shares","buy_date"]].to_dict("records")
                    if not holdings_final.empty else [],
        "capital": {
            "cash": cap_final["cash"],
            "market_value": market_val,
            "total_assets": total_assets,
            "realized_pnl": cap_final["realized_pnl"],
            "unrealized_pnl": market_val - float(cap_final["total_invested"]),
            "total_pnl_pct": pnl_pct
        },
        "no_action_reason": "無符合條件標的" if not actions else ""
    }


if __name__ == "__main__":
    run_daily_simulation()
