"""
TW-Radar 歷史回測引擎
期間：2025/12/04 - 2026/05/31（實際交易日）
資料：FinMind 真實歷史收盤價

嚴格規則（防止未來偏差）：
- 決策日 T：只能用 T 當日及之前的資料計算評分
- 買入價：T 當日收盤價（模擬收盤後下單，T+1 成交更嚴謹，但用收盤簡化）
- 不使用任何未來資料

用法：.venv/bin/python backtest_runner.py
"""

import os, sys, json, sqlite3
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

try:
    import toml
    secrets = toml.load(os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml"))
    os.environ.setdefault("ANTHROPIC_API_KEY", secrets.get("ANTHROPIC_API_KEY", ""))
except Exception:
    pass

from utils.stock_data import get_stock_price, compute_technical_score, get_institutional, get_chip_score
from utils.virtual_investors import INVESTORS, SCAN_LIST

BT_DB = Path(__file__).parent / "data" / "backtest.db"
BACKTEST_START = "2025-12-04"
BACKTEST_END   = "2026-05-30"


def init_bt_db():
    BT_DB.parent.mkdir(exist_ok=True)
    with sqlite3.connect(BT_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bt_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investor_id TEXT,
                stock_id TEXT,
                stock_name TEXT,
                action TEXT,
                price REAL,
                shares REAL,
                pnl REAL DEFAULT 0,
                score INTEGER,
                reason TEXT,
                trade_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bt_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investor_id TEXT,
                date TEXT,
                total_assets REAL,
                cash REAL,
                realized_pnl REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bt_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_id TEXT,
                predict_date TEXT,
                signal TEXT,
                score INTEGER,
                price_signal REAL,
                price_5d REAL,
                result TEXT
            )
        """)
        conn.execute("DELETE FROM bt_trades")
        conn.execute("DELETE FROM bt_daily")
        conn.execute("DELETE FROM bt_predictions")


def get_trading_days(start: str, end: str) -> list:
    """從 FinMind 取得實際交易日（非週末/假日）"""
    df = get_stock_price("2330", days=200)
    if df.empty:
        return []
    df["date_str"] = df["date"].dt.strftime("%Y-%m-%d")
    return sorted([d for d in df["date_str"].tolist()
                   if BACKTEST_START <= d <= BACKTEST_END])


def compute_score_on_date(stock_id: str, price_history: pd.DataFrame, as_of_date: str) -> dict:
    """
    用 as_of_date 當天及之前的資料計算評分
    嚴格不使用未來資料
    """
    # 只用 as_of_date 之前的資料
    hist = price_history[price_history["date"].dt.strftime("%Y-%m-%d") <= as_of_date].copy()
    if len(hist) < 20:
        return {"total": 0, "price": 0, "reason": "資料不足"}

    current_price = float(hist["close"].iloc[-1])

    # 技術面（只看歷史）
    tech = compute_technical_score(hist)
    tech_score = tech.get("score") or 0

    # 籌碼面：用到 as_of_date 的法人資料
    # 注意：回測中不使用法人即時資料（避免延遲偏差），只用技術面
    # 產業面/基本面/先知：回測只用技術面+簡化籌碼，避免前視偏差

    # 簡化版回測評分（只用技術面，最保守不過度擬合）
    total = tech_score * 5  # 技術面20分 × 5 = 最高100分（回測保守版）

    return {
        "total": min(total, 100),
        "price": current_price,
        "tech": tech_score
    }


def run_backtest():
    print("=== TW-Radar 歷史回測 ===")
    print(f"期間：{BACKTEST_START} → {BACKTEST_END}")
    print()

    init_bt_db()

    # 預先載入所有股票的完整歷史
    print("載入歷史資料...")
    price_data = {}
    for sid, sname in SCAN_LIST:
        df = get_stock_price(sid, days=200)
        if not df.empty:
            price_data[sid] = df
            print(f"  {sid} {sname}: {len(df)}筆")

    # 取得交易日序列
    trading_days = get_trading_days(BACKTEST_START, BACKTEST_END)
    print(f"\n交易日: {len(trading_days)} 天 ({trading_days[0]} ~ {trading_days[-1]})")
    print()

    # 三個投資客各自狀態
    states = {
        inv_id: {
            "cash": inv["capital"],
            "holdings": {},  # {stock_id: {"price": float, "shares": float, "score": int, "date": str}}
            "realized_pnl": 0.0,
            "trades": [],
        }
        for inv_id, inv in INVESTORS.items()
    }

    predictions = []

    # 逐日模擬
    for i, date in enumerate(trading_days):
        if i % 10 == 0:
            print(f"[{date}] 模擬中... ({i}/{len(trading_days)})")

        # 當日各股評分
        day_scores = {}
        for sid, sname in SCAN_LIST:
            if sid not in price_data:
                continue
            score_data = compute_score_on_date(sid, price_data[sid], date)
            if score_data.get("price", 0) > 0:
                day_scores[sid] = {**score_data, "name": sname}

                # 記錄預測（5天後驗證）
                signal = "強烈買進" if score_data["total"] >= 75 else \
                         "可考慮買進" if score_data["total"] >= 60 else \
                         "觀察等待" if score_data["total"] >= 45 else "不建議"
                predictions.append({
                    "stock_id": sid,
                    "predict_date": date,
                    "signal": signal,
                    "score": score_data["total"],
                    "price_signal": score_data["price"],
                    "idx": i
                })

        # 三個投資客各自決策
        for inv_id, inv in INVESTORS.items():
            state = states[inv_id]
            strategy = inv["strategy"]

            # 停損/停利/到期 檢查
            to_sell = []
            for sid, pos in state["holdings"].items():
                current_p = day_scores.get(sid, {}).get("price", 0)
                if current_p <= 0:
                    continue
                pnl_pct = (current_p - pos["price"]) / pos["price"]
                days_held = (datetime.strptime(date, "%Y-%m-%d") -
                             datetime.strptime(pos["date"], "%Y-%m-%d")).days
                reason = None
                if pnl_pct <= strategy["stop_loss"]:
                    reason = f"停損{pnl_pct*100:.1f}%"
                elif pnl_pct >= strategy["take_profit"]:
                    reason = f"停利{pnl_pct*100:.1f}%"
                elif days_held >= strategy["hold_days"]:
                    reason = f"持有{days_held}天到期"
                if reason:
                    to_sell.append((sid, current_p, reason))

            for sid, sell_p, reason in to_sell:
                pos = state["holdings"].pop(sid)
                proceeds = sell_p * pos["shares"]
                cost = pos["price"] * pos["shares"]
                pnl = proceeds - cost
                state["cash"] += proceeds
                state["realized_pnl"] += pnl
                state["trades"].append({
                    "inv": inv_id, "sid": sid, "name": pos["name"],
                    "action": "SELL", "price": sell_p,
                    "shares": pos["shares"], "pnl": pnl,
                    "score": pos["score"], "reason": reason, "date": date
                })

            # 買入機會
            held_ids = set(state["holdings"].keys())
            n_pos = len(held_ids)
            candidates = sorted(
                [(sid, d) for sid, d in day_scores.items()
                 if d["total"] >= strategy["min_score"] and sid not in held_ids],
                key=lambda x: x[1]["total"], reverse=True
            )
            for sid, data in candidates:
                if n_pos >= strategy["max_positions"]:
                    break
                if state["cash"] < 3000:
                    break
                price = data["price"]
                budget = min(state["cash"] * strategy["position_size"], state["cash"] * 0.8)
                shares = int(budget / price)
                if shares < 1:
                    continue
                cost = price * shares
                state["cash"] -= cost
                state["holdings"][sid] = {
                    "price": price, "shares": shares,
                    "name": data["name"], "score": data["total"], "date": date
                }
                n_pos += 1
                state["trades"].append({
                    "inv": inv_id, "sid": sid, "name": data["name"],
                    "action": "BUY", "price": price,
                    "shares": shares, "pnl": 0,
                    "score": data["total"], "reason": f"評分{data['total']}", "date": date
                })

            # 記錄每日資產
            market_val = sum(
                day_scores.get(sid, {}).get("price", pos["price"]) * pos["shares"]
                for sid, pos in state["holdings"].items()
            )
            total = state["cash"] + market_val
            with sqlite3.connect(BT_DB) as conn:
                conn.execute(
                    "INSERT INTO bt_daily (investor_id, date, total_assets, cash, realized_pnl) VALUES (?,?,?,?,?)",
                    (inv_id, date, total, state["cash"], state["realized_pnl"])
                )

    # 驗證預測準確率（只驗有5天後資料的）
    print("\n驗證預測準確率...")
    correct = wrong = 0
    for pred in predictions:
        future_idx = pred["idx"] + 5
        if future_idx >= len(trading_days):
            continue
        future_date = trading_days[future_idx]
        sid = pred["stock_id"]
        if sid not in price_data:
            continue
        future_df = price_data[sid][price_data[sid]["date"].dt.strftime("%Y-%m-%d") <= future_date]
        if future_df.empty:
            continue
        future_p = float(future_df["close"].iloc[-1])
        pct = (future_p - pred["price_signal"]) / pred["price_signal"] * 100

        if pred["signal"] in ["強烈買進", "可考慮買進"]:
            result = "正確" if pct > 0.5 else "錯誤"
        elif pred["signal"] == "不建議":
            result = "正確" if pct < -0.5 else "錯誤"
        else:
            result = "中性"

        if result == "正確":
            correct += 1
        elif result == "錯誤":
            wrong += 1

        with sqlite3.connect(BT_DB) as conn:
            conn.execute(
                "INSERT INTO bt_predictions (stock_id, predict_date, signal, score, price_signal, price_5d, result) VALUES (?,?,?,?,?,?,?)",
                (sid, pred["predict_date"], pred["signal"], pred["score"],
                 pred["price_signal"], future_p, result)
            )

    # 儲存交易記錄
    with sqlite3.connect(BT_DB) as conn:
        for inv_id, state in states.items():
            for t in state["trades"]:
                conn.execute(
                    "INSERT INTO bt_trades VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
                    (t["inv"], t["sid"], t["name"], t["action"],
                     t["price"], t["shares"], t["pnl"],
                     t["score"], t["reason"], t["date"])
                )

    # 輸出最終結果
    total_preds = correct + wrong
    accuracy = correct / total_preds * 100 if total_preds > 0 else 0

    print()
    print("=== 回測結果 ===")
    print(f"系統預測準確率: {accuracy:.1f}% ({correct}/{total_preds}筆)")
    print()

    results = {}
    for inv_id, inv in INVESTORS.items():
        state = states[inv_id]
        market_val = sum(
            price_data.get(sid, pd.DataFrame()).tail(1)["close"].values[0] * pos["shares"]
            if not price_data.get(sid, pd.DataFrame()).empty else pos["price"] * pos["shares"]
            for sid, pos in state["holdings"].items()
        )
        total = state["cash"] + market_val
        orig = inv["capital"]
        pnl = total - orig
        pnl_pct = pnl / orig * 100
        n_trades = len([t for t in state["trades"] if t["action"] == "BUY"])

        results[inv_id] = {
            "name": inv["name"],
            "total_assets": total,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "n_trades": n_trades,
            "realized_pnl": state["realized_pnl"]
        }

        print(f"{inv['emoji']} {inv['name']} ({inv['title']})")
        print(f"   總資產: NT${total:,.0f}  損益: NT${pnl:+,.0f} ({pnl_pct:+.1f}%)")
        print(f"   交易次數: {n_trades}  已實現損益: NT${state['realized_pnl']:+,.0f}")
        print()

    # 存報告
    report = {
        "period": f"{BACKTEST_START} to {BACKTEST_END}",
        "trading_days": len(trading_days),
        "accuracy": {"pct": accuracy, "correct": correct, "total": total_preds},
        "investors": results
    }
    report_path = os.path.join(os.path.dirname(__file__), "data", "backtest_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"完整報告: {report_path}")
    return report


if __name__ == "__main__":
    run_backtest()
