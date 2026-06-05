import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import re

# 可靠來源清單（財經+國際，影響股市）
NEWS_SOURCES = [
    # ── 台灣本地（學校網路可通）──
    {
        "name": "Google新聞｜台股",
        "url": "https://news.google.com/rss/search?q=台股+股市+大盤&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "lang": "zh"
    },
    {
        "name": "Google新聞｜科技股",
        "url": "https://news.google.com/rss/search?q=NVIDIA+OR+台積電+OR+聯發科+OR+鴻海+OR+AI晶片&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "lang": "zh"
    },
    {
        "name": "Google新聞｜國際財經",
        "url": "https://news.google.com/rss/search?q=Fed+利率+OR+美股+OR+中美貿易+OR+半導體禁令&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "lang": "zh"
    },
    {
        "name": "經濟日報｜財經",
        "url": "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
        "lang": "zh"
    },
    {
        "name": "經濟日報｜科技",
        "url": "https://money.udn.com/rssfeed/news/1001/5607?ch=money",
        "lang": "zh"
    },
    # ── 國際英文（外網可通）──
    {
        "name": "Yahoo Finance｜頭條",
        "url": "https://finance.yahoo.com/rss/topstories",
        "lang": "en"
    },
    {
        "name": "MarketWatch｜頭條",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "lang": "en"
    },
    {
        "name": "MarketWatch｜市場動態",
        "url": "https://feeds.marketwatch.com/marketwatch/marketpulse/",
        "lang": "en"
    },
    {
        "name": "Investing.com｜財經",
        "url": "https://www.investing.com/rss/news_25.rss",
        "lang": "en"
    },
]

# 影響股市的關鍵字（用來標記重要新聞）
HIGH_IMPACT_KEYWORDS = [
    # 科技巨頭
    "NVIDIA", "AMD", "TSMC", "台積電", "Intel", "Apple", "Google", "Meta", "Microsoft",
    "Samsung", "鴻海", "聯發科", "MediaTek",
    # 市場事件
    "Fed", "聯準會", "升息", "降息", "interest rate", "inflation", "通膨",
    "earnings", "財報", "EPS", "revenue",
    # 地緣政治
    "Taiwan", "台灣", "China", "中國", "trade war", "貿易戰", "tariff", "關稅",
    "semiconductor", "晶片", "chip ban",
    # 市場指標
    "S&P 500", "Nasdaq", "crash", "rally", "崩盤", "大漲", "大跌",
    # AI/科技趨勢
    "AI", "artificial intelligence", "人工智慧", "ChatGPT", "LLM",
]

def fetch_rss(url: str, timeout: int = 8) -> list:
    """擷取單一 RSS 來源"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TW-Radar/1.0)"
        }
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, verify=True)
        except requests.exceptions.SSLError:
            # SSL 攔截環境（企業/學校網路）fallback
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(url, headers=headers, timeout=timeout, verify=False)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        items = []
        # 標準 RSS 2.0
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            desc = item.findtext("description", "").strip()

            if not title or not link:
                continue

            # 解析時間
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date)
                time_str = dt.strftime("%m/%d %H:%M")
            except Exception:
                time_str = ""

            # 判斷是否高影響力
            text_check = (title + desc).upper()
            is_high_impact = any(kw.upper() in text_check for kw in HIGH_IMPACT_KEYWORDS)

            # 清理描述（去HTML tag）
            desc_clean = re.sub(r'<[^>]+>', '', desc).strip()[:120]

            items.append({
                "title": title,
                "link": link,
                "time": time_str,
                "desc": desc_clean,
                "high_impact": is_high_impact,
            })

        return items[:15]  # 每來源最多15條

    except Exception as e:
        return []


def get_all_news(max_per_source: int = 8) -> list:
    """
    擷取所有來源新聞，依時間排序
    只回傳真實資料，無法取得時回傳空清單
    """
    all_news = []

    for source in NEWS_SOURCES:
        items = fetch_rss(source["url"])
        for item in items[:max_per_source]:
            item["source"] = source["name"]
            item["lang"] = source["lang"]
            all_news.append(item)

    # 高影響力排前面，其次依來源順序
    all_news.sort(key=lambda x: (not x["high_impact"]))

    return all_news


def get_high_impact_news() -> list:
    """只回傳高影響力新聞"""
    return [n for n in get_all_news() if n["high_impact"]]
