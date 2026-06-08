import requests
import pandas as pd
from datetime import datetime, timedelta
import time

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = ""  # 填入後可提升到600次/小時

RELIABLE_SOURCES = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^TWII&region=TW&lang=zh-TW",
    "https://www.twse.com.tw/rss/",
]


def _fm_get(dataset: str, stock_id: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    if end_date is None:
        end_date = datetime.today().strftime("%Y-%m-%d")
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    try:
        try:
            resp = requests.get(FINMIND_API, params=params, timeout=10)
        except requests.exceptions.SSLError:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(FINMIND_API, params=params, timeout=10, verify=False)
        data = resp.json()
        if data.get("status") == 200:
            return pd.DataFrame(data["data"])
    except Exception:
        pass
    return pd.DataFrame()


def get_stock_price(stock_id: str, days: int = 90) -> pd.DataFrame:
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = _fm_get("TaiwanStockPrice", stock_id, start)
    if not df.empty and "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    return df


def get_institutional(stock_id: str, days: int = 30) -> pd.DataFrame:
    """三大法人買賣超"""
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return _fm_get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start)


def get_margin_trading(stock_id: str, days: int = 30) -> pd.DataFrame:
    """融資融券"""
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return _fm_get("TaiwanStockMarginPurchaseShortSale", stock_id, start)


def get_stock_info(stock_id: str) -> dict:
    """取得股票基本資訊（名稱）"""
    try:
        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN},
            timeout=10, verify=False
        )
        data = resp.json()
        if data.get("status") == 200:
            df = pd.DataFrame(data["data"])
            row = df[df["stock_id"] == stock_id]
            if not row.empty:
                return {"name": row.iloc[0].get("stock_name", stock_id)}
    except Exception:
        pass
    return {"name": stock_id}


def get_twse_index(days: int = 60) -> dict:
    """
    加權指數（即時 + 歷史走勢）
    即時價：TWSE 官方 API
    歷史：用台積電2330走勢比例代替（FinMind無加權指數歷史）
    """
    result = {"price": None, "change": None, "change_pct": None, "history": pd.DataFrame()}

    # 即時價
    try:
        import requests as _req
        r = _req.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8, verify=False
        )
        item = r.json().get("msgArray", [{}])[0]
        z = float(item.get("z", 0) or 0)
        y = float(item.get("y", 0) or 0)
        if z > 0:
            result["price"] = z
            result["change"] = round(z - y, 2)
            result["change_pct"] = round((z - y) / y * 100, 2) if y else 0
    except Exception:
        pass

    # 歷史走勢用2330近似反映（台積電佔加權指數約27%）
    try:
        df = get_stock_price("2330", days)
        if not df.empty:
            result["history"] = df
    except Exception:
        pass

    return result


def compute_technical_score(df: pd.DataFrame) -> dict:
    """
    技術面評分（滿分20）
    只在有真實資料時計算，不幻想數值
    """
    if df.empty or len(df) < 20:
        return {"score": None, "reason": "資料不足，無法評分", "details": {}}

    close = df["close"].astype(float)
    volume = df["Trading_Volume"].astype(float) if "Trading_Volume" in df.columns else None

    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1] if len(df) >= 60 else None
    current = close.iloc[-1]

    score = 0
    details = {}

    # MA多頭排列
    if current > ma5:
        score += 4
        details["價格>MA5"] = "✅ +4"
    else:
        details["價格>MA5"] = "❌ 0"

    if ma5 > ma20:
        score += 4
        details["MA5>MA20"] = "✅ +4"
    else:
        details["MA5>MA20"] = "❌ 0"

    if ma60 and ma20 > ma60:
        score += 4
        details["MA20>MA60"] = "✅ +4"
    elif ma60 is None:
        details["MA20>MA60"] = "⚪ 資料不足"

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = (100 - 100 / (1 + rs)).iloc[-1]
    if 40 <= rsi <= 70:
        score += 4
        details[f"RSI={rsi:.1f}"] = "✅ +4（健康區間）"
    elif rsi < 40:
        score += 2
        details[f"RSI={rsi:.1f}"] = "🟡 +2（超賣，反彈機會）"
    else:
        details[f"RSI={rsi:.1f}"] = "❌ 0（超買，風險高）"

    # 量價配合
    if volume is not None and len(volume) >= 5:
        recent_vol = volume.iloc[-5:].mean()
        prev_vol = volume.iloc[-20:-5].mean() if len(volume) >= 20 else volume.mean()
        price_up = close.iloc[-1] > close.iloc[-5]
        if price_up and recent_vol > prev_vol:
            score += 4
            details["量增價漲"] = "✅ +4"
        else:
            details["量增價漲"] = "❌ 0"

    return {"score": min(score, 20), "rsi": round(rsi, 1), "details": details,
            "ma5": round(ma5, 2), "ma20": round(ma20, 2), "current": round(current, 2)}


def get_chip_score(institutional_df: pd.DataFrame) -> dict:
    """
    籌碼面評分（滿分20），僅用真實資料
    FinMind 欄位：name(Foreign_Investor/Investment_Trust/Dealer_self)，buy/sell
    """
    if institutional_df.empty:
        return {"score": 0, "reason": "無籌碼資料", "details": {"外資資料": "⚪ 無資料"}}

    score = 0
    details = {}

    # 外資：Foreign_Investor
    fi_names = ["Foreign_Investor", "外陸資買賣超股數(不含外資自營商)", "外資"]
    foreign = pd.DataFrame()
    for fn in fi_names:
        tmp = institutional_df[institutional_df["name"] == fn] if "name" in institutional_df.columns else pd.DataFrame()
        if not tmp.empty:
            foreign = tmp
            break

    if not foreign.empty:
        # 計算淨買超（buy - sell）
        if "buy" in foreign.columns and "sell" in foreign.columns:
            foreign = foreign.copy()
            foreign["net"] = foreign["buy"].astype(float) - foreign["sell"].astype(float)
            recent = foreign.sort_values("date").tail(5)["net"]
        elif "buy_sell" in foreign.columns:
            recent = foreign.sort_values("date").tail(5)["buy_sell"].astype(float)
        else:
            recent = pd.Series(dtype=float)

        if not recent.empty:
            consecutive_buy = (recent > 0).sum()
            net_5d = recent.sum()

            if consecutive_buy >= 4:
                score += 10
                details[f"外資連買{int(consecutive_buy)}日"] = "✅ +10"
            elif consecutive_buy >= 2:
                score += 5
                details[f"外資連買{int(consecutive_buy)}日"] = "🟡 +5"
            else:
                details["外資動向"] = "❌ 0（外資未積極買入）"

            if net_5d > 0:
                score += 5
                details[f"外資5日淨買{net_5d:+,.0f}股"] = "✅ +5"
            else:
                details[f"外資5日淨賣{net_5d:,.0f}股"] = "❌ 0"
        else:
            details["外資資料"] = "⚪ 計算失敗"
    else:
        details["外資資料"] = "⚪ 無法取得"

    # 投信：Investment_Trust
    it_names = ["Investment_Trust", "投信"]
    invest = pd.DataFrame()
    for fn in it_names:
        tmp = institutional_df[institutional_df["name"] == fn] if "name" in institutional_df.columns else pd.DataFrame()
        if not tmp.empty:
            invest = tmp
            break

    if not invest.empty and "buy" in invest.columns:
        invest = invest.copy()
        invest["net"] = invest["buy"].astype(float) - invest["sell"].astype(float)
        it_net = invest.sort_values("date").tail(3)["net"].sum()
        if it_net > 0:
            score += 5
            details[f"投信3日淨買{it_net:+,.0f}股"] = "✅ +5"

    return {"score": min(score, 20), "details": details}


# ── 全市場掃描工具（TWSE 官方 API，免費不限次）──────────────

def get_twse_all_stocks_today() -> pd.DataFrame:
    """
    TWSE 官方 API 一次取得全部上市股票今日行情。
    欄位：stock_id, stock_name, close, change, volume, pe_ratio
    非盤中（盤後）才有完整資料；盤中回傳空 DataFrame。
    """
    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        r = requests.get(
            "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL",
            params={"response": "json", "date": today},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15, verify=False
        )
        data = r.json()
        if data.get("stat") != "OK" or not data.get("data"):
            return pd.DataFrame()
        fields = data.get("fields", [])
        df = pd.DataFrame(data["data"], columns=fields)
        # 標準化欄位名稱
        rename = {
            "證券代號": "stock_id",
            "證券名稱": "stock_name",
            "收盤價":   "close",
            "漲跌價差": "change",
            "成交股數": "volume",
            "本益比":   "pe_ratio",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        # 排除 ETF（代號開頭 00）與權證
        df = df[df["stock_id"].str.match(r"^\d{4}$", na=False)]
        # 數值轉換
        for col in ["close", "change", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", "").str.strip(), errors="coerce"
                )
        return df.dropna(subset=["close"]).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def get_twse_institutional_all(date_str: str = None) -> pd.DataFrame:
    """
    TWSE 三大法人買賣超（全市場，一次取得）。
    回傳 DataFrame：stock_id, foreign_net, invest_net, dealer_net, total_net
    """
    try:
        from datetime import datetime
        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")
        r = requests.get(
            "https://www.twse.com.tw/fund/TWT43U",
            params={"response": "json", "date": date_str, "selectType": "ALLBUT0999"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15, verify=False
        )
        data = r.json()
        if data.get("stat") != "OK" or not data.get("data"):
            return pd.DataFrame()
        fields = data.get("fields", [])
        df = pd.DataFrame(data["data"], columns=fields)
        # 標準化
        rename_map = {}
        for c in fields:
            if "代號" in c or "代碼" in c:
                rename_map[c] = "stock_id"
            elif "外資" in c and "合計" in c:
                rename_map[c] = "foreign_net"
            elif "投信" in c:
                rename_map[c] = "invest_net"
            elif "自營商" in c and "合計" not in c and "自行" not in c:
                rename_map[c] = "dealer_net"
        df = df.rename(columns=rename_map)
        if "stock_id" not in df.columns:
            # 嘗試第一欄為 stock_id
            df = df.rename(columns={fields[0]: "stock_id"})

        df = df[df["stock_id"].str.match(r"^\d{4}$", na=False)]
        for col in ["foreign_net", "invest_net", "dealer_net"]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", "").str.strip(), errors="coerce"
                ).fillna(0)
        # 總淨買
        net_cols = [c for c in ["foreign_net", "invest_net", "dealer_net"] if c in df.columns]
        df["total_net"] = df[net_cols].sum(axis=1)
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()
