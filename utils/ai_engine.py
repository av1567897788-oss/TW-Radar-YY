import anthropic
import json
import os
from datetime import datetime

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _make_client():
    """建立 Anthropic client，處理學校網路 SSL 問題"""
    import httpx, ssl
    try:
        # 先嘗試正常連線
        return anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    except Exception:
        pass
    # SSL bypass fallback（學校/企業網路）
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        http_client = httpx.Client(verify=False)
        return anthropic.Anthropic(api_key=CLAUDE_API_KEY, http_client=http_client)
    except Exception:
        return anthropic.Anthropic(api_key=CLAUDE_API_KEY)


def check_api_balance() -> dict:
    """
    測試 API 是否可用，並偵測餘額狀態
    回傳: {"ok": bool, "status": "ok"|"no_credit"|"no_key"|"error", "msg": str}
    """
    if not CLAUDE_API_KEY:
        return {"ok": False, "status": "no_key", "msg": "未設定 API Key"}
    try:
        import httpx
        client = anthropic.Anthropic(
            api_key=CLAUDE_API_KEY,
            http_client=httpx.Client(verify=False)
        )
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}]
        )
        return {"ok": True, "status": "ok", "msg": "API 正常"}
    except Exception as e:
        err = str(e)
        if "credit" in err.lower() or "balance" in err.lower():
            return {"ok": False, "status": "no_credit", "msg": "餘額不足"}
        if "401" in err or "auth" in err.lower():
            return {"ok": False, "status": "no_key", "msg": "API Key 無效"}
        return {"ok": False, "status": "error", "msg": err[:80]}

CHAT_SYSTEM = """你是 TW-Radar 的專屬台股 AI 顧問「雷達」。

## 你的能力
- 分析台股個股、大盤、產業趨勢
- 解讀財經新聞對股市的影響
- 根據使用者的持股狀況給出個人化建議
- 具備先知能力：從當前資訊預判未來走向

## 核心原則
1. **嚴禁幻想**：每個建議必須基於你收到的真實資料（新聞、數據、持倉）
2. **分析輔助不操控**：告知分析結果和理由，最終買賣由使用者決定
3. **資金=生命**：建議務必保守，停損優先，不鼓勵全押
4. **明確說明來源**：回答時說明「根據今日新聞XXX」或「根據你的持股成本XXX」
5. 若某資訊未提供，直接說「我目前沒有這個資料，建議你...」

## 回答格式
- 繁體中文
- 重點先說，理由後補
- 重要數字加粗
- 建議具體（「可考慮在XXX元加碼」比「可以買進」更有用）
- 有風險必須說"""


def chat_with_radar(
    user_message: str,
    chat_history: list,
    portfolio: list = None,
    news: list = None,
    market_data: dict = None,
    capital: dict = None
) -> str:
    """
    TW-Radar 對話引擎

    chat_history: [{"role": "user"|"assistant", "content": "..."}]
    portfolio: 持股資料
    news: 即時新聞
    market_data: 大盤資訊
    capital: 資金狀況
    """
    if not CLAUDE_API_KEY:
        return "⚠️ 需要 Claude API 才能啟用 AI 對話。請至 console.anthropic.com 儲值後使用。"

    # ── 建立背景資訊（只在第一次或資料更新時帶入）──
    context_parts = []

    if capital:
        available = capital.get("available_cash", 0)
        invested = capital.get("total_invested", 0)
        realized = capital.get("realized_pnl", 0)
        context_parts.append(
            f"【使用者資金狀況】可用現金 NT${available:,.0f} ｜ 持股成本 NT${invested:,.0f} ｜ 已實現損益 NT${realized:+,.0f}"
        )

    if portfolio:
        holdings_txt = []
        for h in portfolio:
            holdings_txt.append(
                f"- {h.get('stock_id')} {h.get('stock_name','')}"
                f" 買入均價{h.get('buy_price',0):.2f}"
                f" 持有{h.get('shares',0):.0f}股"
                f" 現價約{h.get('current_price', h.get('buy_price',0)):.2f}"
                f" 損益{h.get('pnl_pct', 0):+.1f}%"
            )
        if holdings_txt:
            context_parts.append("【使用者持股】\n" + "\n".join(holdings_txt))

    if market_data:
        context_parts.append(
            f"【大盤】加權指數 {market_data.get('price','N/A')} 點，"
            f"今日 {market_data.get('change_pct', 0):+.2f}%"
        )

    if news:
        top_news = [n for n in news if n.get("high_impact")][:8]
        if not top_news:
            top_news = news[:5]
        news_txt = "\n".join(
            f"- [{n.get('source','')}] {n.get('title','')}"
            for n in top_news
        )
        context_parts.append(f"【今日重要新聞】\n{news_txt}")

    # 組裝背景資訊（加在第一條 user message 前）
    context_str = "\n\n".join(context_parts)
    today = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    full_context = f"分析時間：{today}\n\n{context_str}" if context_str else f"分析時間：{today}"

    # ── 組裝訊息串 ──
    messages = []

    # 帶入背景資訊（每次都更新，確保資料最新）
    if chat_history:
        # 在對話歷史開頭插入最新背景
        messages.append({
            "role": "user",
            "content": f"[背景資訊更新]\n{full_context}\n\n以上是我的最新資料，請記住後繼續對話。"
        })
        messages.append({
            "role": "assistant",
            "content": "已更新你的持倉、資金和最新新聞，請繼續提問。"
        })
        # 加入歷史對話（最多10輪）
        messages.extend(chat_history[-20:])
    else:
        # 首次對話
        messages.append({
            "role": "user",
            "content": f"[我的當前狀況]\n{full_context}\n\n以上是我的即時資料。{user_message}"
        })
        try:
            client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                system=CHAT_SYSTEM,
                messages=messages
            )
            return msg.content[0].text
        except Exception as e:
            return _handle_api_error(e)

    # 加入當前問題
    messages.append({"role": "user", "content": user_message})

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=CHAT_SYSTEM,
            messages=messages
        )
        return msg.content[0].text
    except Exception as e:
        return _handle_api_error(e)


def _parse_json_response(content: str) -> dict:
    """從 Claude 回應中穩健解析 JSON，處理 code block 包裹"""
    import re

    # 找第一個 { 到最後一個 } 之間的完整 JSON
    start = content.find("{")
    end = content.rfind("}") + 1
    if start < 0 or end <= start:
        return None

    json_str = content[start:end]

    # 嘗試直接解析
    try:
        return json.loads(json_str)
    except Exception:
        pass

    # 清理控制字元後再試
    try:
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', json_str)
        return json.loads(clean)
    except Exception:
        pass

    return None


def _handle_api_error(e: Exception) -> str:
    err = str(e)
    if "credit" in err.lower() or "balance" in err.lower():
        return "⚠️ Claude API 餘額不足，請至 console.anthropic.com 儲值後使用。"
    if "rate" in err.lower():
        return "⚠️ API 請求過於頻繁，請稍後再試。"
    return f"❌ AI 回應失敗：{err}"

SYSTEM_PROMPT = """你是 TW-Radar 台股先知分析引擎。

核心原則：
1. 嚴禁幻想與憑空捏造。每個建議必須基於提供的真實新聞、數據。
2. 分析輔助，不操控決策。告知數據與理由，最終決策由使用者執行。
3. 資料來源：僅分析提供的新聞標題與量化數據，不引用未提供的消息。
4. 每個結論附「觸發條件達成清單」。
5. 明確說明風險，不單方面鼓勵買進。

回應格式（JSON）：
{
  "verdict": "強烈買進 | 可考慮買進 | 觀察等待 | 不建議 | 建議賣出",
  "confidence": 0-100,
  "triggered_conditions": ["條件1已達成"],
  "not_triggered": ["條件X未達成"],
  "reason": "詳細原因（基於提供的數據）",
  "entry_suggestion": "建議進場時機（若建議買）",
  "stop_loss": "建議停損位置",
  "risk_warning": "主要風險提示",
  "data_basis": ["數據來源1"]
}"""

PROPHET_SYSTEM = """你是 TW-Radar 先知大腦，任務是從即時新聞中提早識別影響台股的信號，比市場早一步做出判斷。

分析原則：
1. 只分析提供的真實新聞，不憑空推測
2. 明確指出哪些新聞對哪些股票/產業有正面或負面影響
3. 識別「市場尚未完全反應」的隱藏信號（這是先知能力的核心）
4. 若新聞訊號不明確，直接說「目前訊號混雜，建議觀望」，不模糊帶過

回應格式（純繁體中文，格式完整）：

## 📡 先知信號判讀
方向：多頭/空頭/混雜，信心度 X/100，持續時間預估

## 🔑 關鍵新聞解讀
逐條列出最重要的3-5則，說明正面或負面影響

## 📈 受惠股與產業
具體列出股票代號+名稱，附上為何受惠的理由

## ⚠️ 潛在地雷
哪些新聞暗藏下跌風險，市場可能尚未意識到

## 🎯 集大成建議（最重要）
這是先知分析的結論，必須回答：
- 未來1-2週整體操作方向（加碼/減碼/觀望）
- 優先布局哪個產業/個股
- 設定何種停損條件
- 新手最容易犯的錯誤提醒
絕對不能只說「看好後市」，必須給出可執行的具體行動建議。"""


def analyze_stock(stock_id: str, stock_name: str, scores: dict, extra_context: str = "",
                  recent_news: list = None, attack_data: dict = None) -> dict:
    """用Claude分析個股，輸入必須是真實計算的分數"""
    if not CLAUDE_API_KEY:
        return {
            "verdict": "無法分析",
            "confidence": 0,
            "reason": "未設定 Claude API Key，請儲值後使用。",
            "triggered_conditions": [],
            "not_triggered": ["API Key未設定或餘額不足"],
            "risk_warning": "請先至 console.anthropic.com 儲值",
            "data_basis": []
        }

    # 相關新聞（只取股票名稱相關的）
    news_section = ""
    if recent_news:
        relevant = [n for n in recent_news if
                    stock_id in n.get("title", "") or
                    stock_name in n.get("title", "") or
                    n.get("high_impact", False)][:8]
        if relevant:
            news_section = "\n\n📰 相關即時新聞（來源可靠）：\n" + "\n".join(
                f"- [{n['source']}] {n['title']}" for n in relevant
            )

    # 計算五層完整分數
    total_score = sum([
        scores.get('technical', {}).get('score') or 0,
        scores.get('chip', {}).get('score') or 0,
        scores.get('fundamental', {}).get('score') or 0,
        scores.get('sector', {}).get('score') or 0,
        scores.get('prophet_stock', {}).get('score') or 0,
    ])

    prompt = f"""分析台股 {stock_id}（{stock_name}）

═══ 五層量化評分（滿分100，真實資料計算）═══
技術面  {scores.get('technical', {}).get('score', 0)}/20 | {json.dumps(scores.get('technical', {}).get('details', {}), ensure_ascii=False)}
籌碼面  {scores.get('chip', {}).get('score', 0)}/20 | {json.dumps(scores.get('chip', {}).get('details', {}), ensure_ascii=False)}
基本面  {scores.get('fundamental', {}).get('score', 0)}/20 | {json.dumps(scores.get('fundamental', {}).get('details', {}), ensure_ascii=False)}
產業面  {scores.get('sector', {}).get('score', 0)}/20 | {json.dumps(scores.get('sector', {}).get('details', {}), ensure_ascii=False)}
AI先知  {scores.get('prophet_stock', {}).get('score', 0)}/20 | {json.dumps(scores.get('prophet_stock', {}).get('details', {}), ensure_ascii=False)}
─────────────────────────────
總分：{total_score}/100
{news_section}
{f'補充：{extra_context}' if extra_context else ''}

必須回傳以下格式的 JSON（直接輸出，不加說明）：
{{
  "verdict": "強烈買進|可考慮買進|觀察等待|不建議|建議賣出",
  "confidence": 0-100的整數,
  "triggered_conditions": ["已達成的條件"],
  "not_triggered": ["未達成的條件"],
  "reason": "分析理由",
  "stop_loss": "建議停損價位",
  "risk_warning": "風險提示",
  "data_basis": ["資料來源"]
}}"""

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, http_client=__import__('httpx').Client(verify=False))
        # 用 Haiku 做結構化輸出（更快、更便宜、更遵從格式）
        # Sonnet 做深度分析
        analysis_msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=600,
            system="你是台股分析引擎。只能輸出純 JSON 物件，絕對不能輸出任何 Markdown、標題、說明文字。",
            messages=[{"role": "user", "content": prompt}]
        )
        content = analysis_msg.content[0].text.strip()
        result = _parse_json_response(content)
        if result:
            return result
        # 備援：直接回傳文字分析結果
        return {"verdict": "分析完成", "reason": content[:400], "confidence": 50,
                "triggered_conditions": [], "not_triggered": [], "risk_warning": "請參閱上方分析", "data_basis": []}
    except Exception as e:
        return {"verdict": "API錯誤", "reason": str(e), "confidence": 0,
                "triggered_conditions": [], "not_triggered": [], "risk_warning": "", "data_basis": []}


def get_prophet_analysis(news_list: list, twii_context: str = "", attack_data: dict = None) -> dict:
    """
    先知大腦：真正把即時新聞餵進 Claude 做深度解讀
    news_list: 從 news_feed.get_all_news() 取得的真實新聞
    """
    if not CLAUDE_API_KEY:
        return {"analysis": "需要 Claude API Key 才能啟用先知分析。請至 console.anthropic.com 儲值後使用。",
                "status": "no_key"}

    if not news_list:
        return {"analysis": "目前無新聞資料可分析，請先點「更新新聞」。", "status": "no_data"}

    # 整理新聞（高影響力優先，最多30條）
    sorted_news = sorted(news_list, key=lambda x: not x.get("high_impact", False))[:30]

    news_text = "\n".join([
        f"{'🔴' if n.get('high_impact') else '•'} [{n.get('source','')}] {n.get('title','')} ({n.get('time','')})"
        for n in sorted_news
    ])

    today = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # 主力攻擊資料
    attack_section = ""
    if attack_data and attack_data.get("summary"):
        attack_section = f"\n\n═══ 主力攻擊偵測（TWSE官方資料）═══\n{attack_data['summary']}"
        if attack_data.get("large_buy"):
            attack_section += "\n\n🔥 外資大買超（可能是布局訊號）：\n" + "\n".join(
                f"  {x['level']} {x['stock_id']} {x['name']} {x['net_shares']/10000:+.0f}萬股"
                for x in attack_data['large_buy'][:8]
            )
        if attack_data.get("large_sell"):
            attack_section += "\n\n🔻 外資大賣超（注意風險）：\n" + "\n".join(
                f"  {x['stock_id']} {x['name']} {x['net_shares']/10000:+.0f}萬股"
                for x in attack_data['large_sell'][:5]
            )
        if attack_data.get("invest_trust_buy"):
            attack_section += "\n\n📈 投信布局：\n" + "\n".join(
                f"  {x['stock_id']} {x['name']}"
                for x in attack_data['invest_trust_buy'][:5]
            )

    prompt = f"""分析時間：{today}

以下是 TW-Radar 系統從可靠財經媒體即時抓取的新聞：

{news_text}
{attack_section}
{'台股加權指數近況：' + twii_context if twii_context else ''}

請作為先知大腦，整合新聞訊號與主力籌碼動向，找出「市場尚未完全反應」的投資機會或風險。
主力攻擊資料是關鍵輔助依據，配合新聞解讀更能提早預判走向。"""

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, http_client=__import__('httpx').Client(verify=False))
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=PROPHET_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        return {
            "analysis": message.content[0].text,
            "status": "ok",
            "news_count": len(sorted_news),
            "timestamp": today
        }
    except Exception as e:
        err = str(e)
        if "credit" in err.lower() or "balance" in err.lower():
            return {"analysis": "⚠️ Claude API 餘額不足，請至 console.anthropic.com 儲值後使用。",
                    "status": "no_credit"}
        if "529" in err or "overload" in err.lower():
            return {"analysis": "⚠️ Claude 伺服器暫時過載（Error 529），請等10秒後再試一次。",
                    "status": "overloaded"}
        return {"analysis": f"分析失敗：{err[:100]}", "status": "error"}
