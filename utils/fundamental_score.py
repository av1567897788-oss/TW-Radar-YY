"""
基本面評分（/20）
來源：FinMind 財務報表 + 月營收
計算：EPS成長、毛利率、營收YoY
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"


def _fm(dataset, stock_id, start_date):
    try:
        r = requests.get(FINMIND_API,
            params={"dataset": dataset, "data_id": stock_id, "start_date": start_date},
            timeout=10, verify=False)
        d = r.json()
        if d.get("status") == 200 and d.get("data"):
            return pd.DataFrame(d["data"])
    except Exception:
        pass
    return pd.DataFrame()


def get_fundamental_score(stock_id: str) -> dict:
    """
    基本面評分（/20）
    - 毛利率趨勢         /6
    - 月營收年增率        /8
    - 稅後淨利趨勢        /6
    """
    score = 0
    details = {}
    start_2y = (datetime.today() - timedelta(days=730)).strftime("%Y-%m-%d")
    start_6m = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")

    # ── 月營收年增率 ────────────────────────────────────
    rev_df = _fm("TaiwanStockMonthRevenue", stock_id, start_2y)
    if not rev_df.empty and "revenue" in rev_df.columns:
        rev_df["revenue"] = rev_df["revenue"].astype(float)
        rev_df = rev_df.sort_values("date")

        # 近3個月營收 vs 去年同期
        if len(rev_df) >= 15:
            recent3 = rev_df["revenue"].tail(3).mean()
            yoy3    = rev_df["revenue"].iloc[-15:-12].mean()
            yoy_pct = (recent3 - yoy3) / yoy3 * 100 if yoy3 > 0 else 0

            if yoy_pct >= 20:
                score += 8
                details[f"月營收YoY +{yoy_pct:.1f}%"] = "✅ +8（高成長）"
            elif yoy_pct >= 5:
                score += 5
                details[f"月營收YoY +{yoy_pct:.1f}%"] = "🟡 +5（穩定成長）"
            elif yoy_pct >= -5:
                score += 2
                details[f"月營收YoY {yoy_pct:+.1f}%"] = "⚪ +2（持平）"
            else:
                details[f"月營收YoY {yoy_pct:+.1f}%"] = "❌ 0（衰退）"
        else:
            details["月營收"] = "⚪ 資料不足"
    else:
        details["月營收"] = "⚪ 無資料"

    # ── 財務報表：毛利率 + 淨利 ──────────────────────────
    fs_df = _fm("TaiwanStockFinancialStatements", stock_id, start_2y)
    if not fs_df.empty and "type" in fs_df.columns:
        # 毛利率
        cogs = fs_df[fs_df["type"] == "CostOfGoodsSold"]["value"].astype(float)
        op_inc = fs_df[fs_df["type"] == "OperatingIncome"]["value"].astype(float)
        rev_fs = fs_df[fs_df["type"] == "Revenue"]["value"].astype(float) \
                 if "Revenue" in fs_df["type"].values else pd.Series(dtype=float)

        if not rev_fs.empty and not cogs.empty:
            gross = (rev_fs.values[-1] - cogs.values[-1]) / rev_fs.values[-1] * 100 \
                    if rev_fs.values[-1] > 0 else 0
            if gross >= 50:
                score += 6
                details[f"毛利率 {gross:.1f}%"] = "✅ +6（高毛利）"
            elif gross >= 30:
                score += 4
                details[f"毛利率 {gross:.1f}%"] = "🟡 +4（中毛利）"
            elif gross >= 15:
                score += 2
                details[f"毛利率 {gross:.1f}%"] = "⚪ +2（低毛利）"
            else:
                details[f"毛利率 {gross:.1f}%"] = "❌ 0（毛利偏低）"
        else:
            details["毛利率"] = "⚪ 無法計算"

        # 淨利趨勢（最近兩季對比）
        net = fs_df[fs_df["type"] == "IncomeAfterTaxes"].sort_values("date")
        if len(net) >= 2:
            latest_net = float(net["value"].iloc[-1])
            prev_net   = float(net["value"].iloc[-2])
            net_chg    = (latest_net - prev_net) / abs(prev_net) * 100 if prev_net != 0 else 0
            if net_chg >= 10:
                score += 6
                details[f"淨利季增 +{net_chg:.1f}%"] = "✅ +6"
            elif net_chg >= 0:
                score += 3
                details[f"淨利季增 +{net_chg:.1f}%"] = "🟡 +3（穩定）"
            else:
                details[f"淨利季減 {net_chg:.1f}%"] = "❌ 0"
        else:
            details["淨利趨勢"] = "⚪ 資料不足"
    else:
        details["財務報表"] = "⚪ 無資料"

    return {"score": min(score, 20), "details": details}
