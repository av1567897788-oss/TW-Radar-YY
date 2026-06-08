"""
TW-Radar 虛擬投資客系統
三位擬人化投資者，模擬真實操作，驗證系統精準度

規則：
- 所有交易使用 FinMind 真實收盤價
- 絕對不造假、不使用未來資料
- 每日收盤後結算，18:00 產出報告
"""

import json
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent.parent / "data" / "simulation.db"

# ══════════════════════════════════════════════════════
# 三位投資客人格設定
# ══════════════════════════════════════════════════════
INVESTORS = {
    "wang": {
        "id": "wang",
        "name": "王建國",
        "age": 42,
        "title": "保守型｜銀行放款主管",
        "personality": (
            "42歲，任職某銀行放款部主管20年。穩健保守，"
            "深知資金安全比獲利重要。只在五層評分極高時才進場，"
            "寧可錯過機會也不要承擔不必要風險。座右銘：『保住本金才能再戰。』"
        ),
        "capital": 50000.0,
        "strategy": {
            "min_score": 75,        # 進場門檻（/100）
            "stop_loss": -0.05,     # 停損 -5%
            "take_profit": 0.10,    # 停利 +10%
            "max_positions": 3,     # 最多3檔
            "position_size": 0.25,  # 每檔不超過25%資金
            "hold_days": 14,        # 最長持有14天
        },
        "emoji": "🧑‍💼"
    },
    "lin": {
        "id": "lin",
        "name": "林志明",
        "age": 39,
        "title": "平衡型｜科技業中階主管",
        "personality": (
            "39歲，電子業PM，月薪9萬。接觸股市10年，"
            "見過多頭也熬過空頭。相信系統分析優於主觀判斷，"
            "平衡配置、分批進出。座右銘：『相信數據，紀律執行。』"
        ),
        "capital": 50000.0,
        "strategy": {
            "min_score": 60,
            "stop_loss": -0.08,
            "take_profit": 0.15,
            "max_positions": 4,
            "position_size": 0.20,
            "hold_days": 20,
        },
        "emoji": "👨‍💻"
    },
    "chen": {
        "id": "chen",
        "name": "陳大海",
        "age": 44,
        "title": "積極型｜自營商老闆",
        "personality": (
            "44歲，開連鎖餐飲創業成功，閒置資金喜歡在股市搏殺。"
            "敢衝敢賭，喜歡趨勢強勢股，相信高風險高報酬。"
            "座右銘：『不搏不富，該出手時就出手。』"
        ),
        "capital": 50000.0,
        "strategy": {
            "min_score": 45,
            "stop_loss": -0.10,
            "take_profit": 0.20,
            "max_positions": 5,
            "position_size": 0.30,
            "hold_days": 10,
        },
        "emoji": "🧔"
    }
}

# 每日掃描標的（25支核心股）
def get_scan_list(attack_data: dict = None) -> list:
    """動態取得掃描清單（全市場篩選，取代靜態 SCAN_LIST）"""
    try:
        from utils.screener import get_dynamic_watch_list
        return get_dynamic_watch_list(attack_data)
    except Exception:
        # fallback 保底清單
        return [
            ("2330","台積電"),("2454","聯發科"),("2382","廣達"),
            ("2317","鴻海"),("3017","奇鋐"),("6669","緯穎"),
            ("3231","緯創"),("2303","聯電"),("2881","富邦金"),
            ("2882","國泰金"),("2002","中鋼"),("2609","陽明"),
            ("2603","長榮"),("6505","台塑化"),("2308","台達電"),
        ]

# 向後相容
SCAN_LIST = get_scan_list()


def init_sim_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_capital (
                investor_id TEXT PRIMARY KEY,
                cash REAL,
                total_invested REAL DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investor_id TEXT,
                stock_id TEXT,
                stock_name TEXT,
                buy_price REAL,
                shares REAL,
                buy_date TEXT,
                score_at_buy INTEGER,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investor_id TEXT,
                stock_id TEXT,
                stock_name TEXT,
                action TEXT,
                price REAL,
                shares REAL,
                pnl REAL DEFAULT 0,
                score_at_action INTEGER,
                reason TEXT,
                trade_date TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT,
                investor_id TEXT,
                report_json TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_id TEXT,
                stock_name TEXT,
                predict_date TEXT,
                signal TEXT,
                score INTEGER,
                price_at_signal REAL,
                price_5d_later REAL,
                result TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # 初始化資金
        for inv_id, inv in INVESTORS.items():
            conn.execute("""
                INSERT OR IGNORE INTO sim_capital (investor_id, cash, total_invested, realized_pnl, updated_at)
                VALUES (?, ?, 0, 0, datetime('now','localtime'))
            """, (inv_id, inv["capital"]))


def get_sim_capital(investor_id: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT cash, total_invested, realized_pnl FROM sim_capital WHERE investor_id=?",
            (investor_id,)
        ).fetchone()
        return {"cash": row[0], "total_invested": row[1], "realized_pnl": row[2]} if row else {}


def get_sim_holdings(investor_id: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql(
            "SELECT * FROM sim_holdings WHERE investor_id=? ORDER BY buy_date DESC",
            conn, params=(investor_id,)
        )


def sim_buy(investor_id: str, stock_id: str, stock_name: str,
            buy_price: float, shares: float, score: int, reason: str, date: str):
    cost = buy_price * shares
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sim_holdings (investor_id, stock_id, stock_name, buy_price, shares, buy_date, score_at_buy) "
            "VALUES (?,?,?,?,?,?,?)",
            (investor_id, stock_id, stock_name, buy_price, shares, date, score)
        )
        conn.execute(
            "UPDATE sim_capital SET cash=cash-?, total_invested=total_invested+?, updated_at=datetime('now','localtime') "
            "WHERE investor_id=?", (cost, cost, investor_id)
        )
        conn.execute(
            "INSERT INTO sim_trades (investor_id, stock_id, stock_name, action, price, shares, score_at_action, reason, trade_date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (investor_id, stock_id, stock_name, "BUY", buy_price, shares, score, reason, date)
        )


def sim_sell(investor_id: str, holding_id: int, stock_id: str, stock_name: str,
             sell_price: float, shares: float, buy_price: float, score: int, reason: str, date: str):
    proceeds = sell_price * shares
    cost = buy_price * shares
    pnl = proceeds - cost
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM sim_holdings WHERE id=?", (holding_id,))
        conn.execute(
            "UPDATE sim_capital SET cash=cash+?, total_invested=total_invested-?, realized_pnl=realized_pnl+?, "
            "updated_at=datetime('now','localtime') WHERE investor_id=?",
            (proceeds, cost, pnl, investor_id)
        )
        conn.execute(
            "INSERT INTO sim_trades (investor_id, stock_id, stock_name, action, price, shares, pnl, score_at_action, reason, trade_date) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (investor_id, stock_id, stock_name, "SELL", sell_price, shares, pnl, score, reason, date)
        )
    return pnl


def save_daily_report(date: str, investor_id: str, report: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sim_daily_reports (report_date, investor_id, report_json) VALUES (?,?,?)",
            (date, investor_id, json.dumps(report, ensure_ascii=False))
        )


def get_all_trades(investor_id: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql(
            "SELECT * FROM sim_trades WHERE investor_id=? ORDER BY trade_date DESC",
            conn, params=(investor_id,)
        )


def get_daily_reports(investor_id: str = None, limit: int = 30) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        if investor_id:
            return pd.read_sql(
                "SELECT * FROM sim_daily_reports WHERE investor_id=? ORDER BY report_date DESC LIMIT ?",
                conn, params=(investor_id, limit)
            )
        return pd.read_sql(
            "SELECT * FROM sim_daily_reports ORDER BY report_date DESC LIMIT ?",
            conn, params=(limit,)
        )


def record_prediction(stock_id: str, name: str, date: str, signal: str, score: int, price: float):
    """記錄預測，5天後自動驗證準確度"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sim_predictions (stock_id, stock_name, predict_date, signal, score, price_at_signal) "
            "VALUES (?,?,?,?,?,?)",
            (stock_id, name, date, signal, score, price)
        )


def verify_predictions():
    """驗證5天前的預測是否正確（真實收盤價）"""
    from utils.stock_data import get_stock_price
    verify_date = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH) as conn:
        pending = conn.execute(
            "SELECT id, stock_id, price_at_signal, signal FROM sim_predictions "
            "WHERE predict_date=? AND result IS NULL",
            (verify_date,)
        ).fetchall()

        for row in pending:
            pid, sid, signal_price, signal = row
            df = get_stock_price(sid, days=2)
            if df.empty:
                continue
            current = float(df["close"].iloc[-1])
            pct = (current - signal_price) / signal_price * 100

            if signal in ["強烈買進", "可考慮買進"]:
                result = "正確" if pct > 1 else "錯誤"
            elif signal == "建議賣出":
                result = "正確" if pct < -1 else "錯誤"
            else:
                result = "中性"

            conn.execute(
                "UPDATE sim_predictions SET price_5d_later=?, result=? WHERE id=?",
                (current, result, pid)
            )


def get_accuracy_stats() -> dict:
    """計算系統預測準確率"""
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(
            "SELECT * FROM sim_predictions WHERE result IS NOT NULL",
            conn
        )
    if df.empty:
        return {"total": 0, "correct": 0, "accuracy": 0}

    total = len(df)
    correct = len(df[df["result"] == "正確"])
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
        "by_signal": df.groupby(["signal", "result"]).size().to_dict()
    }
