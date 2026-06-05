"""
TW-Radar 背景排程器
即使面板沒開啟，仍持續更新資料並寫入 alerts DB

啟動方式：
    .venv/bin/python scheduler.py

會在背景持續執行，不需要面板開著。
"""

import schedule
import time
import sys
import os
import json
from datetime import datetime
sys.path.insert(0, os.path.dirname(__file__))

from utils.database import init_db, get_holdings, get_capital, add_alert
from utils.stock_data import get_stock_price, compute_technical_score, get_chip_score, get_institutional
from utils.news_feed import get_all_news
from utils.ai_engine import get_prophet_analysis

# ── 快取（避免重複呼叫 API）──────────────────────────────
_news_cache = []
_news_updated = None

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def is_market_hours() -> bool:
    """是否為台股盤中時間（平日 9:00-13:30）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return (9, 0) <= (now.hour, now.minute) <= (13, 30)


# ══════════════════════════════════════════════
# JOB 1：更新新聞（每30分鐘）
# ══════════════════════════════════════════════
def job_update_news():
    global _news_cache, _news_updated
    log("📰 更新新聞...")
    try:
        news = get_all_news(max_per_source=8)
        _news_cache = news
        _news_updated = datetime.now()
        high = [n for n in news if n["high_impact"]]
        log(f"  取得 {len(news)} 條，高影響力 {len(high)} 條")

        # 高影響力新聞 → 寫入警示
        for n in high[:3]:
            add_alert("MARKET", "news_impact",
                      f"🔴 重要新聞：{n['title'][:60]}", "warning")
    except Exception as e:
        log(f"  新聞更新失敗：{e}")


# ══════════════════════════════════════════════
# JOB 2：盤中股價監控（每10分鐘，盤中才跑）
# ══════════════════════════════════════════════
def job_monitor_holdings():
    if not is_market_hours():
        return

    log("📊 盤中監控持股...")
    holdings = get_holdings()
    if holdings.empty:
        return

    for _, row in holdings.iterrows():
        sid = row["stock_id"]
        buy_price = float(row["buy_price"])
        shares = int(row["shares"])

        try:
            df = get_stock_price(sid, days=2)
            if df.empty or "close" not in df.columns:
                continue
            current = float(df["close"].iloc[-1])
            pnl_pct = (current - buy_price) / buy_price * 100

            # 停損警告 -8%
            if pnl_pct <= -8:
                add_alert(sid, "stop_loss",
                          f"🔴 【{sid}】跌幅 {pnl_pct:.1f}%，已觸及停損線 -8%，建議立即賣出！",
                          "danger")
                log(f"  🚨 {sid} 觸及停損 {pnl_pct:.1f}%")
            # 注意 -4%
            elif pnl_pct <= -4:
                add_alert(sid, "watch",
                          f"🟡 【{sid}】跌幅 {pnl_pct:.1f}%，需要注意",
                          "warning")
                log(f"  ⚠️  {sid} 注意 {pnl_pct:.1f}%")
            # 大漲提醒（+10% 考慮停利）
            elif pnl_pct >= 10:
                add_alert(sid, "take_profit",
                          f"🟢 【{sid}】漲幅 {pnl_pct:.1f}%，可考慮部分停利",
                          "info")
                log(f"  💰 {sid} 漲幅 {pnl_pct:.1f}%，停利提醒")
        except Exception as e:
            log(f"  {sid} 監控失敗：{e}")


# ══════════════════════════════════════════════
# JOB 3：每日技術評分（收盤後 14:00）
# ══════════════════════════════════════════════
def job_daily_technical():
    log("📈 計算持股技術評分...")
    holdings = get_holdings()
    if holdings.empty:
        return

    for _, row in holdings.iterrows():
        sid = row["stock_id"]
        try:
            df = get_stock_price(sid, days=90)
            score_data = compute_technical_score(df)
            score = score_data.get("score")
            rsi = score_data.get("rsi")
            if score is not None:
                if score <= 8:
                    add_alert(sid, "technical_weak",
                              f"⚠️ 【{sid}】技術面評分偏低（{score}/20），RSI={rsi}，建議減碼或觀望",
                              "warning")
                elif score >= 16:
                    add_alert(sid, "technical_strong",
                              f"✅ 【{sid}】技術面強勢（{score}/20），RSI={rsi}，可持續持有",
                              "info")
                log(f"  {sid} 技術分={score}/20 RSI={rsi}")
        except Exception as e:
            log(f"  {sid} 技術評分失敗：{e}")


# ══════════════════════════════════════════════
# JOB 4：先知分析（每日 9:00 & 20:00）
# ══════════════════════════════════════════════
def job_prophet():
    log("🔮 執行先知分析...")
    global _news_cache

    if not _news_cache:
        job_update_news()

    if not _news_cache:
        log("  無新聞資料，跳過")
        return

    try:
        result = get_prophet_analysis(_news_cache)
        status = result.get("status", "")
        if status == "ok":
            analysis = result.get("analysis", "")
            # 擷取前150字作為摘要寫入 alert
            summary = analysis[:150].replace("\n", " ")
            add_alert("MARKET", "prophet",
                      f"🔮 先知分析更新：{summary}...",
                      "info")
            # 完整分析存到檔案（讓面板讀取）
            with open("data/prophet_latest.json", "w", encoding="utf-8") as f:
                json.dump({
                    "analysis": analysis,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "news_count": result.get("news_count", 0)
                }, f, ensure_ascii=False)
            log(f"  先知分析完成，{result.get('news_count',0)} 條新聞")
        elif status == "no_credit":
            log("  Claude API 餘額不足，跳過")
        else:
            log(f"  先知分析失敗：{status}")
    except Exception as e:
        log(f"  先知分析錯誤：{e}")


# ══════════════════════════════════════════════
# JOB 5：溫馨提醒（每日 8:30，開盤前）
# ══════════════════════════════════════════════
def job_morning_reminder():
    log("☀️ 生成晨間溫馨提醒...")
    holdings = get_holdings()
    capital = get_capital()

    reminders = []

    # 資金安全檢查
    available = float(capital.get("available_cash", 0))
    invested = float(capital.get("total_invested", 0))
    total = available + invested
    lifeline = 50000 * 0.3  # 15000

    if total < lifeline * 1.2:  # 距離生命線 20% 內
        reminders.append(f"🚨 資金警告：總資產 NT${total:,.0f}，距生命線 NT${lifeline:,.0f} 僅剩 {((total-lifeline)/lifeline*100):.0f}%！請謹慎操作。")

    # 持股提醒
    if not holdings.empty:
        reminders.append(f"📋 今日持股 {len(holdings)} 支，請注意停損線（買入價-8%）。")
        for _, row in holdings.iterrows():
            sid = row["stock_id"]
            buy = float(row["buy_price"])
            stop = buy * 0.92
            reminders.append(f"   • {sid}：停損位 {stop:.1f}，買入價 {buy:.1f}")
    else:
        reminders.append("📋 目前無持股，可關注面板「推薦股」尋找機會。")

    # 寫入提醒
    if reminders:
        msg = "\n".join(reminders)
        add_alert("SYSTEM", "morning_reminder", msg, "info")
        log(f"  晨間提醒完成，{len(reminders)} 條")


# ══════════════════════════════════════════════
# 排程設定
# ══════════════════════════════════════════════
def setup_schedule():
    # 新聞：每30分鐘
    schedule.every(30).minutes.do(job_update_news)

    # 盤中監控：每10分鐘
    schedule.every(10).minutes.do(job_monitor_holdings)

    # 技術評分：每日 19:50（晚間，外網環境）
    schedule.every().day.at("19:50").do(job_daily_technical)

    # 虛擬投資客模擬：每日 20:00（晚間，外網環境）
    def job_virtual_sim():
        log("🤖 執行虛擬投資客模擬...")
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from sim_runner import run_daily_simulation
            run_daily_simulation()
        except Exception as e:
            log(f"  模擬失敗：{e}")
    schedule.every().day.at("20:00").do(job_virtual_sim)

    # 先知分析：每日 9:00 & 20:00
    schedule.every().day.at("09:00").do(job_prophet)
    schedule.every().day.at("20:00").do(job_prophet)

    # 晨間提醒：每日 8:30
    schedule.every().day.at("08:30").do(job_morning_reminder)

    log("✅ 排程設定完成：")
    log("   新聞 → 每30分鐘")
    log("   持股監控 → 每10分鐘（盤中）")
    log("   技術評分 → 每日 19:50")
    log("   先知分析 → 每日 9:00 & 20:00")
    log("   晨間提醒 → 每日 8:30")


if __name__ == "__main__":
    init_db()
    log("🚀 TW-Radar 背景排程器啟動")

    setup_schedule()

    # 啟動時先跑一次
    log("🔁 啟動初始化掃描...")
    job_update_news()
    job_monitor_holdings()

    log("⏳ 持續監控中（Ctrl+C 停止）...")
    while True:
        schedule.run_pending()
        time.sleep(60)
