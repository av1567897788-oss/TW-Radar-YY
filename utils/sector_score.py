"""
產業面評分（/20）
來源：Google News RSS 產業關鍵字 + 主力攻擊偵測
"""

import requests
import xml.etree.ElementTree as ET
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TW-Radar/1.0)"}

# 股票代號對應產業關鍵字
SECTOR_MAP = {
    # AI/半導體
    "2330": ["台積電", "半導體", "晶圓代工", "AI晶片", "先進製程"],
    "2454": ["聯發科", "手機晶片", "AI", "MediaTek"],
    "2303": ["聯電", "成熟製程", "晶圓代工"],
    "3034": ["聯詠", "驅動IC", "顯示"],
    # AI伺服器
    "2382": ["廣達", "AI伺服器", "雲端", "NVIDIA"],
    "2317": ["鴻海", "電動車", "AI伺服器", "iPhone"],
    "6669": ["緯穎", "伺服器", "雲端"],
    "3231": ["緯創", "伺服器", "AI"],
    "3017": ["奇鋐", "散熱", "AI伺服器"],
    # 金融
    "2881": ["富邦金", "升息", "金融股"],
    "2882": ["國泰金", "保險", "金融"],
    "2891": ["中信金", "銀行", "金融"],
    # 傳產
    "2002": ["中鋼", "鋼鐵", "基建"],
    "2609": ["陽明", "航運", "貨運"],
    "2615": ["萬海", "航運"],
    "2603": ["長榮", "航運"],
}

# 台股整體景氣關鍵字（正面/負面）
POSITIVE_SECTOR = [
    "AI需求強勁", "半導體復甦", "台積電擴產", "伺服器需求",
    "NVIDIA合作", "美國投資", "AI基礎建設", "晶片法案",
    "升息暫停", "降息預期", "景氣回溫"
]
NEGATIVE_SECTOR = [
    "景氣下行", "需求疲弱", "庫存去化", "裁員",
    "中美貿易戰", "晶片禁令", "通膨惡化", "升息加速",
    "衰退風險", "訂單取消"
]


def _fetch_news(keyword: str, max_items: int = 10) -> list:
    url = f"https://news.google.com/rss/search?q={keyword}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, verify=False)
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", "")
            items.append(title)
        return items
    except Exception:
        return []


def get_sector_score(stock_id: str, attack_data: dict = None) -> dict:
    """
    產業面評分（/20）
    - 產業新聞情緒    /10
    - 主力攻擊加成    /6
    - 大盤方向加成    /4
    """
    score = 0
    details = {}

    # 取得該股產業關鍵字
    keywords = SECTOR_MAP.get(stock_id, [stock_id])

    # 搜尋最相關關鍵字新聞
    all_titles = []
    primary_kw = keywords[0] if keywords else stock_id
    news = _fetch_news(primary_kw + " 股市", max_items=15)
    all_titles.extend(news)

    # 計算正負情緒
    pos_count = sum(1 for t in all_titles for kw in POSITIVE_SECTOR if kw in t)
    neg_count = sum(1 for t in all_titles for kw in NEGATIVE_SECTOR if kw in t)
    net_sentiment = pos_count - neg_count

    if net_sentiment >= 3:
        score += 10
        details[f"產業訊號：{pos_count}正/{neg_count}負"] = "✅ +10（強烈利多）"
    elif net_sentiment >= 1:
        score += 7
        details[f"產業訊號：{pos_count}正/{neg_count}負"] = "🟡 +7（偏多）"
    elif net_sentiment == 0:
        score += 4
        details["產業訊號：中性"] = "⚪ +4（無明顯方向）"
    else:
        score += 1
        details[f"產業訊號：{pos_count}正/{neg_count}負"] = "❌ +1（偏空）"

    # 主力攻擊加成
    if attack_data:
        # 該股是否在攻擊清單中
        buy_ids = [x["stock_id"] for x in attack_data.get("large_buy", [])]
        sell_ids = [x["stock_id"] for x in attack_data.get("large_sell", [])]

        if stock_id in buy_ids:
            atk = next(x for x in attack_data["large_buy"] if x["stock_id"] == stock_id)
            if "大型攻擊" in atk["level"]:
                score += 6
                details[f"主力攻擊{atk['net_shares']/10000:.0f}萬股"] = "✅ +6（外資大量進場）"
            else:
                score += 3
                details[f"主力攻擊{atk['net_shares']/10000:.0f}萬股"] = "🟡 +3（外資買超）"
        elif stock_id in sell_ids:
            details["外資大賣"] = "❌ 0（外資撤退）"

    # 大盤方向加成
    if attack_data:
        direction = attack_data.get("market_direction", "")
        if "多頭" in direction:
            score += 4
            details["大盤方向：多頭"] = "✅ +4"
        elif "空頭" in direction:
            details["大盤方向：空頭"] = "❌ 0"
        else:
            score += 2
            details["大盤方向：混雜"] = "⚪ +2"

    return {"score": min(score, 20), "details": details}
