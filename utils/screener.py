"""
TW-Radar 選股引擎 v2
- 推薦股：五層100分評分篩選
- 堪憂股：偵測技術面惡化 + 籌碼逃跑訊號
- 動態擴充：依市場熱門題材自動調整掃描清單

資料來源：FinMind（真實資料，不幻想）
"""

import pandas as pd
from datetime import datetime
from utils.stock_data import (
    get_stock_price, get_institutional, compute_technical_score, get_chip_score,
    get_twse_all_stocks_today, get_twse_institutional_all
)
from utils.fundamental_score import get_fundamental_score
from utils.sector_score import get_sector_score
from utils.prophet_score import get_prophet_stock_score

# ── 基礎核心清單（長期關注的重要股）────────────────────────
CORE_LIST = [
    # 半導體/AI核心
    ("2330", "台積電"), ("2303", "聯電"), ("2454", "聯發科"),
    ("3034", "聯詠"), ("6770", "力積電"), ("2337", "旺宏"),
    # AI伺服器/散熱題材
    ("3231", "緯創"), ("2382", "廣達"), ("2356", "英業達"),
    ("2317", "鴻海"), ("6669", "緯穎"), ("3017", "奇鋐"),
    # 金融股
    ("2881", "富邦金"), ("2882", "國泰金"), ("2891", "中信金"),
    ("2886", "兆豐金"), ("2884", "玉山金"),
    # 傳產/電動車
    ("2002", "中鋼"), ("1301", "台塑"), ("1303", "南亞"),
    ("2207", "和泰車"),
    # 電信
    ("2412", "中華電"), ("3045", "台灣大"),
    # 航運（景氣循環）
    ("2609", "陽明"), ("2603", "長榮"),
    # 其他科技
    ("2308", "台達電"), ("3008", "大立光"), ("2912", "統一超"),
    ("6505", "台塑化"), ("2357", "華碩"),
]

# ── 動態熱門題材清單（依市場熱點輪替）─────────────────────
# 由 get_dynamic_watch_list() 自動從新聞/主力攻擊偵測動態補充
DYNAMIC_TOPICS = {
    "AI伺服器":     [("3044","健鼎"),("5269","祥碩"),("3707","漢磊")],
    "CoWoS封裝":    [("3711","日月光投控"),("5347","世界先進")],
    "電動車":       [("2049","上銀"),("1590","亞德客")],
    "航太國防":     [("2634","漢翔"),("2612","中航")],
    "生技醫療":     [("6547","配客嘉"),("4938","和碩")],
}

# 動態清單快取（避免每次重算）
_dynamic_cache = None
_dynamic_cache_time = None


def get_dynamic_watch_list(attack_data: dict = None) -> list:
    """
    全市場兩階段篩選：
    Stage 1 - TWSE 官方 API 一次取得所有股票行情 + 三大法人買賣超
             → 快速預篩前 80 支候選（漲幅、量能、外資買超、各產業代表）
    Stage 2 - 五層深度評分（在 get_recommended_stocks 執行）

    快取 10 分鐘，避免重複呼叫 TWSE
    """
    global _dynamic_cache, _dynamic_cache_time

    if _dynamic_cache and _dynamic_cache_time and \
       (datetime.now() - _dynamic_cache_time).seconds < 600:
        return _dynamic_cache

    seen_ids = set()
    result = []

    def add(sid, sname):
        if sid not in seen_ids and sid.isdigit() and len(sid) == 4:
            seen_ids.add(sid)
            result.append((sid, sname or sid))

    # ── 核心清單（保底，確保主要股一定掃到）──────────────
    for sid, sname in CORE_LIST:
        add(sid, sname)

    # ── Stage 1A：TWSE 今日全市場行情預篩 ─────────────────
    try:
        all_df = get_twse_all_stocks_today()
        if not all_df.empty and "close" in all_df.columns:
            # 1. 漲幅前 20（今日強勢股）
            if "change" in all_df.columns:
                top_gain = all_df.nlargest(20, "change")
                for _, row in top_gain.iterrows():
                    add(str(row["stock_id"]), str(row.get("stock_name", "")))

            # 2. 成交量前 20（市場熱度最高）
            if "volume" in all_df.columns:
                top_vol = all_df.nlargest(20, "volume")
                for _, row in top_vol.iterrows():
                    add(str(row["stock_id"]), str(row.get("stock_name", "")))
    except Exception:
        pass

    # ── Stage 1B：三大法人買超前 20 ───────────────────────
    try:
        inst_df = get_twse_institutional_all()
        if not inst_df.empty and "total_net" in inst_df.columns:
            top_inst = inst_df.nlargest(20, "total_net")
            # 先從 all_df 補名稱，若已有就用
            for _, row in top_inst.iterrows():
                sid = str(row["stock_id"])
                sname = ""
                if "all_df" in dir() and not all_df.empty:
                    nm = all_df[all_df["stock_id"] == sid]
                    if not nm.empty and "stock_name" in nm.columns:
                        sname = str(nm.iloc[0]["stock_name"])
                add(sid, sname)
    except Exception:
        pass

    # ── Stage 1C：主力攻擊偵測補充（attack_detector）────────
    if attack_data and attack_data.get("large_buy"):
        for item in attack_data["large_buy"][:15]:
            add(str(item["stock_id"]), item.get("name", ""))

    # ── 各產業補充（確保全產業覆蓋）──────────────────────
    SECTOR_SEEDS = [
        # 生技醫療
        ("4726","東洋"),("1790","庚申"),("4737","華廣"),("6547","北極星藥業"),
        # 能源/綠能
        ("3576","聯合再生"),("6213","聯茂"),("6176","瑞儀"),
        # 房地產
        ("2597","潤弘"),("5522","遠雄"),
        # 觀光餐飲
        ("2727","王品"),("2706","第一店"),
        # 傳產化工
        ("1326","台化"),("1402","遠東新"),
        # 電子零組件
        ("2379","瑞昱"),("2385","群光"),("3231","緯創"),
        # 光電/面板
        ("2412","中華電"),("3481","群創"),("2409","友達"),
    ]
    for sid, sname in SECTOR_SEEDS:
        add(sid, sname)

    # 最多 100 支（避免單次掃描太久）
    result = result[:100]

    _dynamic_cache = result
    _dynamic_cache_time = datetime.now()
    return result


def score_stock_full(stock_id: str, stock_name: str, attack_data: dict = None,
                     news_list: list = None) -> dict:
    """
    五層完整評分（滿分100分）
    - 技術面 /20
    - 籌碼面 /20
    - 基本面 /20
    - 產業面 /20
    - AI先知 /20
    """
    try:
        price_df = get_stock_price(stock_id, days=90)
        chip_df  = get_institutional(stock_id, days=30)

        if price_df.empty or len(price_df) < 20:
            return None

        tech  = compute_technical_score(price_df)
        chip  = get_chip_score(chip_df)
        fund  = get_fundamental_score(stock_id)
        sect  = get_sector_score(stock_id, attack_data)
        proph = get_prophet_stock_score(stock_id, news_list, attack_data)

        tech_score  = tech.get("score")  or 0
        chip_score  = chip.get("score")  or 0
        fund_score  = fund.get("score")  or 0
        sect_score  = sect.get("score")  or 0
        proph_score = proph.get("score") or 0

        total = tech_score + chip_score + fund_score + sect_score + proph_score  # 最高100

        return {
            "stock_id":    stock_id,
            "stock_name":  stock_name,
            "total_score": total,
            "tech_score":  tech_score,
            "chip_score":  chip_score,
            "fund_score":  fund_score,
            "sect_score":  sect_score,
            "proph_score": proph_score,
            "current_price": tech.get("current", 0),
            "rsi":   tech.get("rsi", 50),
            "ma5":   tech.get("ma5",  0),
            "ma20":  tech.get("ma20", 0),
            "tech_details":  tech.get("details",  {}),
            "chip_details":  chip.get("details",  {}),
            "fund_details":  fund.get("details",  {}),
            "sect_details":  sect.get("details",  {}),
            "proph_details": proph.get("details", {}),
        }
    except Exception as e:
        return None


def get_recommended_stocks(top_n: int = 10, attack_data: dict = None,
                           news_list: list = None) -> list:
    """
    推薦股：五層100分評分，由高到低排列
    動態監控清單 = 核心清單 + 當日主力大買清單
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    watch_list = get_dynamic_watch_list(attack_data)
    results = []

    def _score(item):
        sid, sname = item
        return score_stock_full(sid, sname, attack_data, news_list)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_score, item): item for item in watch_list}
        for future in as_completed(futures):
            data = future.result()
            if data:
                results.append(data)

    results.sort(key=lambda x: x["total_score"], reverse=True)

    for i, r in enumerate(results[:top_n]):
        reasons = []
        if r["tech_score"]  >= 16: reasons.append("技術面強勢")
        if r["chip_score"]  >= 12: reasons.append("外資積極進場")
        if r["fund_score"]  >= 12: reasons.append("基本面亮眼")
        if r["sect_score"]  >= 12: reasons.append("產業題材熱")
        if r["proph_score"] >= 10: reasons.append("主力攻擊訊號")
        if r["rsi"] and r["rsi"] < 40: reasons.append(f"超賣反彈機會(RSI={r['rsi']:.0f})")
        r["reasons"] = reasons if reasons else ["多項指標達標"]
        r["rank"] = i + 1
        # 五層細分顯示
        r["score_breakdown"] = (
            f"技術{r['tech_score']} 籌碼{r['chip_score']} "
            f"基本{r['fund_score']} 產業{r['sect_score']} 先知{r['proph_score']}"
        )

    return results[:top_n]


def get_warning_stocks(holdings_df: pd.DataFrame = None,
                       attack_data: dict = None, news_list: list = None) -> list:
    """
    堪憂股：兩類
    1. 使用者持股中有問題的（優先顯示）
    2. 監控清單中技術面惡化的股票
    """
    warnings = []
    watch_list = get_dynamic_watch_list(attack_data)

    # 先檢查使用者持股
    if holdings_df is not None and not holdings_df.empty:
        for _, row in holdings_df.iterrows():
            sid   = row["stock_id"]
            sname = row.get("stock_name", sid) or sid
            buy_p = float(row["buy_price"])

            data = score_stock_full(sid, sname, attack_data, news_list)
            if not data:
                continue

            current  = data["current_price"]
            pnl_pct  = (current - buy_p) / buy_p * 100 if buy_p else 0
            risk_reasons = []
            urgency = 0

            if pnl_pct <= -8:
                risk_reasons.append(f"⛔ 已觸停損（虧損{abs(pnl_pct):.1f}%）")
                urgency += 10
            elif pnl_pct <= -4:
                risk_reasons.append(f"⚠️ 接近停損線（虧損{abs(pnl_pct):.1f}%）")
                urgency += 5

            if data["tech_score"] <= 6:
                risk_reasons.append("技術面嚴重惡化")
                urgency += 4
            elif data["tech_score"] <= 10:
                risk_reasons.append("技術面轉弱")
                urgency += 2

            if data["rsi"] and data["rsi"] > 75:
                risk_reasons.append(f"RSI超買{data['rsi']:.0f}，回檔風險")
                urgency += 3

            if data["chip_score"] <= 4:
                risk_reasons.append("外資賣超，籌碼流失")
                urgency += 3

            # 主力大賣清單
            if attack_data:
                sell_ids = [x["stock_id"] for x in attack_data.get("large_sell", [])]
                if sid in sell_ids:
                    risk_reasons.append("今日外資大量賣超")
                    urgency += 4

            if risk_reasons:
                warnings.append({
                    **data,
                    "risk_reasons": risk_reasons,
                    "urgency": urgency,
                    "is_holding": True,
                    "buy_price": buy_p,
                    "pnl_pct": pnl_pct,
                    "action": "立即賣出" if urgency >= 8 else "考慮減碼" if urgency >= 4 else "密切觀察"
                })

    # 掃描監控清單（不含持股）
    held_ids = set(holdings_df["stock_id"].tolist()) if holdings_df is not None and not holdings_df.empty else set()

    for sid, sname in watch_list:
        if sid in held_ids:
            continue
        data = score_stock_full(sid, sname, attack_data, news_list)
        if not data:
            continue

        risk_reasons = []
        urgency = 0

        if data["tech_score"] <= 4:
            risk_reasons.append("技術面崩壞（均線全面空頭）")
            urgency += 5
        if data["rsi"] and data["rsi"] > 78:
            risk_reasons.append(f"RSI嚴重超買({data['rsi']:.0f})")
            urgency += 3
        if data["chip_score"] == 0:
            risk_reasons.append("外資連續大賣")
            urgency += 4
        if attack_data:
            sell_ids = [x["stock_id"] for x in attack_data.get("large_sell", [])]
            if sid in sell_ids:
                risk_reasons.append("今日外資攻擊性賣超")
                urgency += 5

        if risk_reasons and urgency >= 4:
            warnings.append({
                **data,
                "risk_reasons": risk_reasons,
                "urgency": urgency,
                "is_holding": False,
                "buy_price": None,
                "pnl_pct": None,
                "action": "避免進場"
            })

    warnings.sort(key=lambda x: (not x["is_holding"], -x["urgency"]))
    return warnings[:15]
