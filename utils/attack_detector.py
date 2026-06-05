"""
主力攻擊偵測模組
來源：TWSE 官方 API（免費），每日更新

定義「主力攻擊」（基於歷史分位數）：
- 外資單日淨買超 ≥ 2000萬股 → 大型攻擊
- 外資單日淨買超 ≥ 500萬股  → 中型攻擊
- 投信連買3日以上             → 投信布局
- 自營商大買（對沖需求）       → 輔助訊號
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TW-Radar/1.0)"}

# 攻擊門檻（根據台股歷史統計）
THRESHOLD_LARGE_ATTACK = 20_000_000    # 2000萬股 = 大型攻擊
THRESHOLD_MEDIUM_ATTACK = 5_000_000    # 500萬股  = 中型攻擊
THRESHOLD_INVEST_TRUST = 1_000_000     # 100萬股  = 投信布局


def get_latest_trading_date() -> str:
    """找最近有資料的交易日（往前最多找7天）"""
    d = datetime.today()
    for _ in range(7):
        date_str = d.strftime("%Y%m%d")
        try:
            r = requests.get(
                f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL",
                headers=HEADERS, timeout=8, verify=False
            )
            j = r.json()
            if j.get("stat") == "OK" and len(j.get("data", [])) > 100:
                return date_str
        except Exception:
            pass
        d -= timedelta(days=1)
    return datetime.today().strftime("%Y%m%d")


def get_twse_institutional(date_str: str = None) -> pd.DataFrame:
    """取得全市場三大法人買賣超（TWSE T86）"""
    date = date_str or get_today_str()
    try:
        r = requests.get(
            f"https://www.twse.com.tw/fund/T86?response=json&date={date}&selectType=ALL",
            headers=HEADERS, timeout=10, verify=False
        )
        d = r.json()
        if d.get("data"):
            df = pd.DataFrame(d["data"], columns=d["fields"])
            return df
    except Exception:
        pass
    return pd.DataFrame()


def detect_attacks(date_str: str = None) -> dict:
    """
    偵測主力攻擊訊號
    date_str: YYYYMMDD，不傳則自動找最近交易日
    """
    """
    偵測今日主力攻擊訊號
    回傳：{
        "date": "2026-06-02",
        "large_buy": [{"stock_id","name","net_shares","level"}],
        "large_sell": [...],
        "invest_trust_buy": [...],
        "summary": "文字摘要",
        "market_direction": "多/空/混雜"
    }
    """
    date = date_str or get_latest_trading_date()
    df = get_twse_institutional(date)

    result = {
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "large_buy": [],
        "large_sell": [],
        "invest_trust_buy": [],
        "summary": "",
        "market_direction": "混雜"
    }

    if df.empty:
        result["summary"] = f"⚠️ 無法取得 {date} 法人資料（可能非交易日）"
        return result

    def safe_int(val):
        try:
            return int(str(val).replace(",", "").replace(" ", ""))
        except Exception:
            return 0

    # 外資買賣超
    foreign_col = "外陸資買賣超股數(不含外資自營商)"
    invest_col = "投信買賣超股數"
    dealer_col = "自營商買賣超股數(自行買賣)"

    if foreign_col in df.columns:
        df["net_foreign"] = df[foreign_col].apply(safe_int)
        df["stock_id"] = df["證券代號"].str.strip()
        df["name"] = df["證券名稱"].str.strip()

        # 大買超（攻擊）
        buy_df = df[df["net_foreign"] >= THRESHOLD_MEDIUM_ATTACK].nlargest(10, "net_foreign")
        for _, row in buy_df.iterrows():
            level = "🔥大型攻擊" if row["net_foreign"] >= THRESHOLD_LARGE_ATTACK else "⚡中型攻擊"
            result["large_buy"].append({
                "stock_id": row["stock_id"],
                "name": row["name"],
                "net_shares": row["net_foreign"],
                "level": level
            })

        # 大賣超（撤退）
        sell_df = df[df["net_foreign"] <= -THRESHOLD_MEDIUM_ATTACK].nsmallest(5, "net_foreign")
        for _, row in sell_df.iterrows():
            result["large_sell"].append({
                "stock_id": row["stock_id"],
                "name": row["name"],
                "net_shares": row["net_foreign"],
                "level": "🔻大量撤退"
            })

    # 投信布局
    if invest_col in df.columns:
        df["net_invest"] = df[invest_col].apply(safe_int)
        it_df = df[df["net_invest"] >= THRESHOLD_INVEST_TRUST].nlargest(5, "net_invest")
        for _, row in it_df.iterrows():
            result["invest_trust_buy"].append({
                "stock_id": row["stock_id"],
                "name": row["name"],
                "net_shares": row["net_invest"],
                "level": "📈投信布局"
            })

    # 市場方向判斷
    total_foreign_net = df["net_foreign"].sum() if "net_foreign" in df.columns else 0
    if total_foreign_net > 500_000_000:
        result["market_direction"] = "多頭（外資大量淨買入）"
    elif total_foreign_net < -500_000_000:
        result["market_direction"] = "空頭（外資大量淨賣出）"
    else:
        result["market_direction"] = "混雜（方向不明）"

    # 文字摘要（供先知大腦使用）
    buy_summary = "、".join([f"{x['stock_id']}{x['name']}({x['level']})" for x in result["large_buy"][:5]])
    sell_summary = "、".join([f"{x['stock_id']}{x['name']}" for x in result["large_sell"][:3]])
    it_summary = "、".join([f"{x['stock_id']}{x['name']}" for x in result["invest_trust_buy"][:3]])

    result["summary"] = (
        f"【{result['date']} 主力動向】\n"
        f"市場方向：{result['market_direction']}\n"
        f"外資攻擊（買超）：{buy_summary or '無顯著大單'}\n"
        f"外資撤退（賣超）：{sell_summary or '無大幅賣壓'}\n"
        f"投信布局：{it_summary or '無明顯動作'}\n"
        f"外資全市場淨額：{total_foreign_net:+,.0f} 股"
    )

    return result


def get_stock_attack_signal(stock_id: str, days: int = 5) -> dict:
    """
    偵測特定股票的主力攻擊訊號（近N日）
    結合三大法人連續買超判斷
    """
    from utils.stock_data import get_institutional, get_stock_price
    import math

    chip_df = get_institutional(stock_id, days=days * 3)
    price_df = get_stock_price(stock_id, days=days * 3)

    signals = []
    score_add = 0

    if not chip_df.empty and "buy" in chip_df.columns:
        # 外資連買日數
        fi = chip_df[chip_df["name"] == "Foreign_Investor"].copy()
        if not fi.empty:
            fi["net"] = fi["buy"].astype(float) - fi["sell"].astype(float)
            recent = fi.sort_values("date").tail(days)
            consec = (recent["net"] > 0).sum()
            net_total = recent["net"].sum()

            if consec >= 4:
                signals.append(f"🔥 外資連買{int(consec)}日，近{days}日淨買{net_total:+,.0f}股")
                score_add += 15
            elif consec >= 2:
                signals.append(f"⚡ 外資連買{int(consec)}日")
                score_add += 8

    # 成交量異常（量能爆增）
    if not price_df.empty and "Trading_Volume" in price_df.columns:
        price_df["vol"] = price_df["Trading_Volume"].astype(float)
        recent_vol = price_df["vol"].tail(3).mean()
        hist_vol = price_df["vol"].iloc[:-3].mean() if len(price_df) > 3 else recent_vol
        vol_ratio = recent_vol / hist_vol if hist_vol > 0 else 1

        if vol_ratio >= 3:
            signals.append(f"🔥 量能暴增 {vol_ratio:.1f}倍（短線主力介入跡象）")
            score_add += 10
        elif vol_ratio >= 2:
            signals.append(f"⚡ 量能放大 {vol_ratio:.1f}倍")
            score_add += 5

    return {
        "signals": signals,
        "score_bonus": min(score_add, 20),
        "has_attack": score_add >= 10
    }
