"""
AI先知評分（/20）
整合：主力攻擊訊號 + 量能異常 + 新聞情緒量化
"""

import pandas as pd
from utils.attack_detector import get_stock_attack_signal


def get_prophet_stock_score(stock_id: str, news_list: list = None, attack_data: dict = None) -> dict:
    """
    AI先知評分（/20）
    - 主力攻擊信號      /10（量能爆增 + 外資連買）
    - 新聞情緒分數      /6（相關新聞正負比）
    - 市場動能加成      /4（大盤方向配合度）
    """
    score = 0
    details = {}

    # ── 主力攻擊信號（量能 + 外資）────────────────────────
    atk = get_stock_attack_signal(stock_id, days=5)
    if atk["has_attack"]:
        score += atk["score_bonus"]
        for sig in atk["signals"]:
            details[sig] = "✅"
    elif atk["score_bonus"] > 0:
        score += atk["score_bonus"]
        for sig in atk["signals"]:
            details[sig] = "🟡"
    else:
        details["主力信號"] = "⚪ 無明顯主力介入"

    # ── 新聞情緒量化 ──────────────────────────────────────
    if news_list:
        # 找與該股相關的新聞
        STOCK_NAMES_ZH = {
            "2330": ["台積電", "TSMC"], "2454": ["聯發科", "MediaTek"],
            "2382": ["廣達"], "2317": ["鴻海"], "2303": ["聯電"],
            "6669": ["緯穎"], "3017": ["奇鋐"], "3231": ["緯創"],
            "2881": ["富邦金"], "2882": ["國泰金"],
        }
        names = STOCK_NAMES_ZH.get(stock_id, [stock_id])

        relevant = [n for n in news_list if any(nm in n.get("title", "") for nm in names)]
        high_impact_rel = [n for n in relevant if n.get("high_impact")]

        if len(high_impact_rel) >= 3:
            score += 6
            details[f"相關重大新聞{len(high_impact_rel)}條"] = "✅ +6"
        elif len(high_impact_rel) >= 1:
            score += 3
            details[f"相關新聞{len(high_impact_rel)}條"] = "🟡 +3"
        elif len(relevant) >= 2:
            score += 1
            details[f"一般相關新聞{len(relevant)}條"] = "⚪ +1"
        else:
            details["相關新聞"] = "⚪ 無特定新聞"

    # ── 主力攻擊清單加成（該股在今日大買清單）────────────
    if attack_data:
        buy_ids = [x["stock_id"] for x in attack_data.get("large_buy", [])]
        if stock_id in buy_ids:
            x = next(i for i in attack_data["large_buy"] if i["stock_id"] == stock_id)
            score += 4
            details[f"今日外資攻擊榜{x['level']}"] = "✅ +4"

    return {"score": min(score, 20), "details": details}
