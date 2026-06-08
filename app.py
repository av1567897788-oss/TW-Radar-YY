import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))

from utils.database import (
    init_db, get_holdings, add_holding, sell_holding, remove_holding,
    get_capital, update_available_cash, get_unread_alerts, mark_alerts_read,
    get_trade_history
)
from utils.stock_data import get_stock_price, get_institutional, compute_technical_score, get_chip_score, get_twse_index
from utils.ai_engine import analyze_stock, get_prophet_analysis, chat_with_radar, check_api_balance
from utils.news_feed import get_all_news
from streamlit_autorefresh import st_autorefresh
from utils.screener import get_recommended_stocks, get_warning_stocks
from utils.attack_detector import detect_attacks

# ── 初始化 ──────────────────────────────────────────────
st.set_page_config(
    page_title="TW-Radar 台股雷達站",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

init_db()

# ── 自動刷新（盤中5分鐘，盤外30分鐘）──────────────────
_now = datetime.utcnow() + timedelta(hours=8)  # 台灣時間 UTC+8
_is_market = (_now.weekday() < 5) and (9, 0) <= (_now.hour, _now.minute) <= (13, 30)
_refresh_ms = 5 * 60 * 1000 if _is_market else 30 * 60 * 1000
st_autorefresh(interval=_refresh_ms, key="autorefresh")

ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

ORIGINAL_CAPITAL = 50000.0
LIFELINE = ORIGINAL_CAPITAL * 0.30  # NT$15,000

# ── CSS 樣式 ────────────────────────────────────────────
st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 0.5rem; }
    .metric-card {
        background: #1A1D27; border-radius: 10px; padding: 16px;
        border-left: 4px solid #00D4AA; margin-bottom: 8px;
    }
    .alert-red { border-left-color: #FF4B4B !important; }
    .alert-yellow { border-left-color: #FFD700 !important; }
    .status-green { color: #00D4AA; font-weight: bold; }
    .status-red { color: #FF4B4B; font-weight: bold; }
    .status-yellow { color: #FFD700; font-weight: bold; }
    .prophet-box {
        background: #1A1D27; border-radius: 10px; padding: 16px;
        border: 1px solid #00D4AA; margin-bottom: 12px;
    }
    h1 { font-size: 1.4rem !important; }
    h2 { font-size: 1.1rem !important; }
    h3 { font-size: 0.95rem !important; }
    .stButton > button { border-radius: 8px; }
    div[data-testid="metric-container"] { background: #1A1D27; border-radius: 8px; padding: 8px; }
</style>
""", unsafe_allow_html=True)

# ── 密碼驗證 ────────────────────────────────────────────
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if st.session_state.authenticated:
        return True
    st.markdown("## 📡 TW-Radar 台股雷達站")
    password = st.text_input("請輸入密碼", type="password", key="pwd_input")
    if st.button("登入"):
        correct = st.secrets.get("auth", {}).get("password", "changeme")
        if password == correct:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    return False

if not check_password():
    st.stop()

# ── 標題列 ──────────────────────────────────────────────
col_title, col_api, col_time = st.columns([3, 1, 1])
with col_title:
    st.markdown("# 📡 TW-Radar 台股雷達站")
with col_time:
    _tw_now = datetime.utcnow() + timedelta(hours=8)  # UTC+8 台灣時間
    st.markdown(f"<div style='text-align:right; color:#888; padding-top:10px;'>{_tw_now.strftime('%Y/%m/%d %H:%M')} (台灣)</div>", unsafe_allow_html=True)

# ── API 狀態偵測（每頁面載入檢查一次，快取10分鐘）──────
with col_api:
    api_cache_key = "api_status_cache"
    api_time_key = "api_status_time"
    now_ts = datetime.now()
    cache_expired = (now_ts - st.session_state.get(api_time_key, datetime.min)).seconds > 600

    if cache_expired or api_cache_key not in st.session_state:
        st.session_state[api_cache_key] = check_api_balance()
        st.session_state[api_time_key] = now_ts

    api_status = st.session_state[api_cache_key]

    if api_status["status"] == "ok":
        st.markdown("<div style='text-align:right; padding-top:10px;'>🟢 <span style='color:#00D4AA; font-size:0.8rem;'>AI 正常</span></div>", unsafe_allow_html=True)
    elif api_status["status"] == "no_credit":
        st.markdown("<div style='text-align:right; padding-top:10px;'>🔴 <span style='color:#FF4B4B; font-size:0.8rem;'>API 餘額不足</span></div>", unsafe_allow_html=True)
    elif api_status["status"] == "no_key":
        st.markdown("<div style='text-align:right; padding-top:10px;'>⚪ <span style='color:#888; font-size:0.8rem;'>未設定Key</span></div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='text-align:right; padding-top:10px;'>🟡 <span style='color:#FFD700; font-size:0.8rem;'>API 異常</span></div>", unsafe_allow_html=True)

# ── 網路狀態自動偵測（快取5分鐘，顯示各 API 可用性）──────
_net_cache_key = "net_status_cache"
_net_time_key  = "net_status_time"
_net_expired   = (datetime.now() - st.session_state.get(_net_time_key, datetime.min)).seconds > 300

if _net_expired or _net_cache_key not in st.session_state:
    import requests as _req

    def _probe(url, timeout=4):
        try:
            r = _req.get(url, timeout=timeout, verify=False,
                         headers={"User-Agent": "Mozilla/5.0"})
            return r.status_code < 400
        except Exception:
            return False

    st.session_state[_net_cache_key] = {
        "twse":    _probe("https://mis.twse.com.tw"),
        "finmind": _probe("https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"),
        "general": _probe("https://www.google.com"),
    }
    st.session_state[_net_time_key] = datetime.now()

_ns = st.session_state.get(_net_cache_key, {})
_net_parts = []
if _ns.get("general"):  _net_parts.append("🌐 外網")
if _ns.get("twse"):     _net_parts.append("🏦 TWSE")
if _ns.get("finmind"):  _net_parts.append("📊 FinMind")
if _net_parts:
    _net_bar_color = "#00D4AA" if _ns.get("finmind") else "#FFD700"
    st.markdown(
        f"<div style='font-size:0.72rem; color:{_net_bar_color}; "
        f"text-align:right; padding-bottom:4px;'>"
        f"{'　'.join(_net_parts)}</div>",
        unsafe_allow_html=True
    )
else:
    st.markdown("<div style='font-size:0.72rem; color:#FF4B4B; text-align:right;'>❌ 網路異常</div>",
                unsafe_allow_html=True)

# ── 餘額不足警示橫幅 ────────────────────────────────────
if api_status["status"] == "no_credit":
    st.markdown("""
    <div style='background:#2A1010; border:1px solid #FF4B4B; border-radius:8px; padding:12px 16px; margin-bottom:8px;'>
        <b style='color:#FF4B4B;'>⚠️ Claude API 餘額不足，AI 分析功能暫停</b>
        <span style='color:#888; font-size:0.85rem;'>（先知分析、個股AI分析、雷達對話均無法使用）</span>
    </div>
    """, unsafe_allow_html=True)
    if st.button("💳 前往儲值 API Credits", key="goto_billing"):
        st.markdown("[點此開啟儲值頁面](https://console.anthropic.com/settings/billing){:target='_blank'}", unsafe_allow_html=True)
        import webbrowser
        webbrowser.open("https://console.anthropic.com/settings/billing")

st.divider()

# ── 全局資料預載（供所有欄位使用）──────────────────────
holdings = get_holdings()
capital = get_capital()

# ── 預先載入所有持股現價進快取（避免後續顯示0）──────────
for _, _row in holdings.iterrows():
    _sid = _row["stock_id"]
    _ck = f"price_{_sid}"
    _tk = f"{_ck}_time"
    if _ck not in st.session_state or \
       (datetime.now() - st.session_state.get(_tk, datetime.min)).seconds > 300:
        _df = get_stock_price(_sid, days=5)
        _price = float(_df["close"].iloc[-1]) if not _df.empty and "close" in _df.columns else float(_row["buy_price"])
        st.session_state[_ck] = _price
        st.session_state[_tk] = datetime.now()

# ── 股票代號查詢函數 ────────────────────────────────────
STOCK_NAME_MAP = {
    "2330":"台積電","2303":"聯電","2454":"聯發科","3034":"聯詠",
    "6770":"力積電","2337":"旺宏","3231":"緯創","2382":"廣達",
    "2356":"英業達","2317":"鴻海","6669":"緯穎","3017":"奇鋐",
    "2881":"富邦金","2882":"國泰金","2891":"中信金","2886":"兆豐金",
    "2884":"玉山金","2002":"中鋼","1301":"台塑","1303":"南亞",
    "2207":"和泰車","2412":"中華電","3045":"台灣大","4904":"遠傳",
    "2303":"聯電","6953":"緯湃","2308":"台達電","3008":"大立光",
    "2912":"統一超","1216":"統一","2105":"正新","2609":"陽明",
    "2615":"萬海","2603":"長榮","6505":"台塑化",
}

@st.cache_data(ttl=86400)
def _load_all_stock_names() -> dict:
    """
    全台股名稱對照表（雙來源）：
    優先 TWSE 官方（學校網路可用），備用 FinMind
    """
    import requests as _req

    # ── 來源1：TWSE STOCK_DAY_ALL（一次取所有上市股名稱）──
    try:
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y%m%d")
        r = _req.get(
            "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL",
            params={"response": "json", "date": today},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10, verify=False
        )
        data = r.json()
        if data.get("stat") == "OK" and data.get("data"):
            fields = data.get("fields", [])
            df = pd.DataFrame(data["data"], columns=fields)
            id_col = next((c for c in fields if "代號" in c or "代碼" in c), None)
            nm_col = next((c for c in fields if "名稱" in c), None)
            if id_col and nm_col:
                result = dict(zip(df[id_col].str.strip(), df[nm_col].str.strip()))
                if len(result) > 100:
                    return result
    except Exception:
        pass

    # ── 來源2：FinMind（備用）───────────────────────────────
    try:
        resp = _req.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockInfo"},
            timeout=15, verify=False
        )
        data = resp.json()
        if data.get("status") == 200:
            df = pd.DataFrame(data["data"])
            if "stock_id" in df.columns and "stock_name" in df.columns:
                return dict(zip(df["stock_id"], df["stock_name"]))
    except Exception:
        pass
    return {}


def get_twse_realtime_info(stock_id: str) -> dict:
    """
    TWSE 即時個股查詢（學校網路可用）。
    回傳 {"name": str, "price": float}
    """
    import requests as _req
    try:
        r = _req.get(
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch=tse_{stock_id}.tw&json=1&delay=0",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8, verify=False
        )
        item = r.json().get("msgArray", [{}])[0]
        name  = item.get("n", "").strip()
        price = float(item.get("z", 0) or item.get("y", 0) or 0)
        return {"name": name, "price": price}
    except Exception:
        pass
    # 嘗試 OTC（上櫃）
    try:
        r = _req.get(
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch=otc_{stock_id}.tw&json=1&delay=0",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8, verify=False
        )
        item = r.json().get("msgArray", [{}])[0]
        name  = item.get("n", "").strip()
        price = float(item.get("z", 0) or item.get("y", 0) or 0)
        return {"name": name, "price": price}
    except Exception:
        pass
    return {"name": "", "price": 0.0}


def get_dynamic_stock_name(stock_id: str) -> str:
    """優先靜態 map → TWSE 即時 → FinMind 全表"""
    name = STOCK_NAME_MAP.get(stock_id, "")
    if name:
        return name
    # TWSE 即時查詢（快速且學校網路可用）
    info = get_twse_realtime_info(stock_id)
    if info.get("name"):
        return info["name"]
    # 最後備用：FinMind 全表
    all_names = _load_all_stock_names()
    return all_names.get(stock_id, "")


def get_stock_current_price(stock_id: str) -> float:
    """取得股票最新價（FinMind → TWSE 即時雙來源）"""
    ck = f"price_{stock_id}"
    if ck in st.session_state:
        return st.session_state[ck]
    # 先試 FinMind
    df = get_stock_price(stock_id, days=5)
    if not df.empty and "close" in df.columns:
        price = float(df["close"].iloc[-1])
        st.session_state[ck] = price
        st.session_state[f"{ck}_time"] = datetime.now()
        return price
    # FinMind 失敗 → fallback TWSE 即時（學校網路友善）
    info = get_twse_realtime_info(stock_id)
    if info.get("price", 0) > 0:
        price = info["price"]
        st.session_state[ck] = price
        st.session_state[f"{ck}_time"] = datetime.now()
        return price
    return 0.0

# ── 警示列（全寬）──────────────────────────────────────
alerts_df = get_unread_alerts()
if not alerts_df.empty:
    # 只顯示股票相關警示，過濾掉 news_impact（新聞在底部統一顯示）
    stock_alerts = alerts_df[~alerts_df["alert_type"].isin(["news_impact"])]
    if not stock_alerts.empty:
        for _, alert in stock_alerts.iterrows():
            level_icon = "🔴" if alert["level"] == "danger" else "🟡"
            card_cls = "alert-red" if alert["level"] == "danger" else "alert-yellow"
            st.markdown(f"<div class='metric-card {card_cls}'>"
                        f"{level_icon} <b>[{alert['stock_id']}]</b> {alert['message']}</div>",
                        unsafe_allow_html=True)
        if st.button("✓ 標記已讀", key="mark_read"):
            mark_alerts_read()
            st.rerun()

# ── 主要三欄佈局 ────────────────────────────────────────
col_market, col_portfolio, col_capital = st.columns([2, 1.5, 1.5])

# ╔══════════════════════════════╗
# ║  左欄：市場行情               ║
# ╚══════════════════════════════╝
with col_market:
    st.markdown("## 📈 市場行情")

    # 加權指數
    with st.spinner("載入加權指數..."):
        twii_data = get_twse_index(days=60)

    twii_df = twii_data.get("history", pd.DataFrame())
    twii_price = twii_data.get("price")
    twii_change = twii_data.get("change", 0) or 0
    twii_pct = twii_data.get("change_pct", 0) or 0

    if twii_price:
        color = "#FF4B4B" if twii_change >= 0 else "#22C55E"  # 台股慣例：漲紅跌綠
        arrow = "▲" if twii_change >= 0 else "▼"
        st.markdown(f"""
        <div class='metric-card'>
            <div style='font-size:0.85rem; color:#888;'>加權指數 TAIEX（即時）</div>
            <div style='font-size:1.8rem; font-weight:bold; color:{color};'>
                {twii_price:,.2f}
                <span style='font-size:1rem;'>{arrow} {abs(twii_change):,.2f} ({twii_pct:+.2f}%)</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.info("⚠️ 加權指數暫無即時資料（非盤中或網路問題）")

    # 走勢圖（以台積電近期走勢呈現大盤趨勢）
    if not twii_df.empty and "close" in twii_df.columns:
        fig = go.Figure()
        if all(c in twii_df.columns for c in ["open", "max", "min", "close"]):
            fig.add_trace(go.Candlestick(
                x=twii_df["date"],
                open=twii_df["open"].astype(float),
                high=twii_df["max"].astype(float),
                low=twii_df["min"].astype(float),
                close=twii_df["close"].astype(float),
                name="2330",
                increasing_line_color="#FF4B4B",   # 台股慣例：漲=紅
                decreasing_line_color="#22C55E"    # 台股慣例：跌=綠
            ))
        else:
            fig.add_trace(go.Scatter(
                x=twii_df["date"], y=twii_df["close"].astype(float),
                line=dict(color="#00D4AA", width=2), name="台積電走勢"
            ))
        fig.update_layout(
            height=240, margin=dict(l=0, r=0, t=5, b=0),
            plot_bgcolor="#0E1117", paper_bgcolor="#0E1117",
            font=dict(color="#FAFAFA", size=10),
            xaxis=dict(showgrid=False, rangeslider_visible=False),
            yaxis=dict(showgrid=True, gridcolor="#2A2D3A"),
            showlegend=False
        )
        st.plotly_chart(fig, use_container_width=True)
        st.markdown("<div style='color:#444; font-size:0.7rem;'>* K線顯示台積電(2330)走勢，與大盤高度相關</div>", unsafe_allow_html=True)

    # ── 我的持股即時行情（有持股才顯示）────────────────────
    if not holdings.empty:
        st.markdown("---")
        st.markdown("### 📊 持股即時行情")
        for _, row in holdings.iterrows():
            sid = row["stock_id"]
            sname = row.get("stock_name", sid) or sid
            buy_price = float(row["buy_price"])
            shares = float(row["shares"])

            # 取最新價
            cache_key = f"price_{sid}"
            if cache_key not in st.session_state or \
               (datetime.now() - st.session_state.get(f"{cache_key}_time", datetime.min)).seconds > 300:
                df_p = get_stock_price(sid, days=5)
                cur = float(df_p["close"].iloc[-1]) if not df_p.empty and "close" in df_p.columns else buy_price
                st.session_state[cache_key] = cur
                st.session_state[f"{cache_key}_time"] = datetime.now()
            else:
                cur = st.session_state[cache_key]

            chg = cur - buy_price
            chg_pct = chg / buy_price * 100 if buy_price else 0
            color = "#FF4B4B" if chg >= 0 else "#22C55E"  # 台股慣例：漲紅跌綠
            arrow = "▲" if chg >= 0 else "▼"

            st.markdown(f"""
            <div style='display:flex; justify-content:space-between; padding:6px 10px;
                        background:#1A1D27; border-radius:6px; margin-bottom:4px;'>
                <span style='color:#FAFAFA; font-size:0.88rem;'><b>{sid}</b> {sname}</span>
                <span style='color:{color}; font-size:0.88rem; font-weight:bold;'>
                    {cur:.1f} {arrow}{abs(chg_pct):.1f}%
                </span>
            </div>
            """, unsafe_allow_html=True)

    # 個股查詢（提問窗口）
    st.markdown("---")
    st.markdown("## 🔍 個股分析提問")
    query_col1, query_col2 = st.columns([2, 1])
    with query_col1:
        query_stock = st.text_input("輸入股票代號（例：2330）", key="query_stock", placeholder="2330")
    with query_col2:
        extra_ctx = st.text_input("補充背景（可略）", key="extra_ctx", placeholder="例：最近宣布擴廠")

    if st.button("🧠 分析這支股票", key="analyze_btn"):
        if query_stock:
            with st.spinner(f"分析 {query_stock} 中（五層評分計算）..."):
                from utils.fundamental_score import get_fundamental_score
                from utils.sector_score import get_sector_score
                from utils.prophet_score import get_prophet_stock_score

                price_df = get_stock_price(query_stock, days=90)
                chip_df  = get_institutional(query_stock, days=30)
                tech     = compute_technical_score(price_df)
                chip     = get_chip_score(chip_df)
                fund     = get_fundamental_score(query_stock)
                atk_data = st.session_state.get("atk_cache") or detect_attacks()
                st.session_state["atk_cache"] = atk_data
                sect     = get_sector_score(query_stock, atk_data)
                proph    = get_prophet_stock_score(
                    query_stock,
                    news_list=st.session_state.get("news_data", []),
                    attack_data=atk_data
                )

                scores = {
                    "technical": tech, "chip": chip,
                    "fundamental": fund, "sector": sect,
                    "prophet_stock": proph
                }

                result = analyze_stock(
                    query_stock,
                    get_dynamic_stock_name(query_stock) or query_stock,
                    scores, extra_ctx,
                    recent_news=st.session_state.get("news_data", []),
                    attack_data=atk_data
                )

            # 顯示結果
            verdict = result.get("verdict", "未知")
            confidence = result.get("confidence", 0)
            verdict_color = {
                "強烈買進": "#00D4AA", "可考慮買進": "#90EE90",
                "觀察等待": "#FFD700", "不建議": "#FF8C00",
                "建議賣出": "#FF4B4B"
            }.get(verdict, "#FAFAFA")

            st.markdown(f"""
            <div class='prophet-box'>
                <div style='font-size:1.2rem; font-weight:bold; color:{verdict_color};'>
                    {verdict} &nbsp; 信心度：{confidence}%
                </div>
            </div>
            """, unsafe_allow_html=True)

            # 技術面細項
            if tech.get("score") is not None:
                with st.expander(f"📊 技術面評分：{tech['score']}/20", expanded=True):
                    for k, v in tech.get("details", {}).items():
                        st.write(f"• {k}：{v}")
                    if "rsi" in tech:
                        st.write(f"• RSI：{tech['rsi']}")
                    if "current" in tech:
                        st.write(f"• 現價：{tech['current']}  MA5：{tech.get('ma5')}  MA20：{tech.get('ma20')}")

            # 籌碼面細項
            if chip.get("score") is not None:
                with st.expander(f"💰 籌碼面評分：{chip['score']}/20"):
                    for k, v in chip.get("details", {}).items():
                        st.write(f"• {k}：{v}")

            # AI 結論
            with st.expander("🤖 AI 分析詳情", expanded=True):
                triggered = result.get("triggered_conditions", [])
                not_triggered = result.get("not_triggered", [])
                if triggered:
                    st.markdown("**已達成條件：**")
                    for c in triggered:
                        st.write(f"  ✅ {c}")
                if not_triggered:
                    st.markdown("**未達成條件：**")
                    for c in not_triggered:
                        st.write(f"  ❌ {c}")
                if result.get("reason"):
                    st.markdown(f"**分析理由：** {result['reason']}")
                if result.get("stop_loss"):
                    st.markdown(f"**⚠️ 建議停損：** {result['stop_loss']}")
                if result.get("risk_warning"):
                    st.markdown(f"**🚨 風險提示：** {result['risk_warning']}")
        else:
            st.warning("請輸入股票代號")

    # ── AI 對話視窗 ──────────────────────────────────────
    st.markdown("---")
    st.markdown("## 💬 雷達 AI 對話")
    st.markdown("<div style='color:#888; font-size:0.8rem; margin-bottom:8px;'>可問：「台積電能買嗎？」「今天新聞對哪支股票有利？」「我的持股要注意什麼？」</div>",
                unsafe_allow_html=True)

    # 初始化對話記憶
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # 顯示對話歷史
    chat_container = st.container()
    with chat_container:
        for msg in st.session_state.chat_history:
            if msg["role"] == "user":
                st.markdown(f"""
                <div style='background:#2A2D3A; border-radius:10px; padding:10px 14px; margin:4px 0; text-align:right;'>
                    <span style='color:#FAFAFA; font-size:0.88rem;'>🧑 {msg["content"]}</span>
                </div>""", unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div style='background:#1A1D27; border-left:3px solid #00D4AA; border-radius:10px; padding:10px 14px; margin:4px 0;'>
                    <span style='color:#FAFAFA; font-size:0.85rem; white-space:pre-wrap;'>📡 {msg["content"]}</span>
                </div>""", unsafe_allow_html=True)

    # 輸入框
    chat_input = st.text_input(
        "問雷達...",
        placeholder="例：2330現在值得買嗎？今天有什麼重要消息？我的持股有風險嗎？",
        key="chat_input_box"
    )

    chat_col1, chat_col2 = st.columns([1, 1])
    with chat_col1:
        send_btn = st.button("📨 送出問題", key="chat_send")
    with chat_col2:
        if st.button("🗑 清除對話", key="chat_clear"):
            st.session_state.chat_history = []
            st.rerun()

    if send_btn and chat_input.strip():
        # 準備背景資料
        portfolio_ctx = []
        if not holdings.empty:
            for _, row in holdings.iterrows():
                sid = row["stock_id"]
                bp = float(row["buy_price"])
                sh = float(row["shares"])
                cp = st.session_state.get(f"price_{sid}", bp)
                portfolio_ctx.append({
                    "stock_id": sid,
                    "stock_name": row.get("stock_name", sid),
                    "buy_price": bp,
                    "shares": sh,
                    "current_price": cp,
                    "pnl_pct": (cp - bp) / bp * 100 if bp else 0
                })

        market_ctx = {}
        if twii_price:
            market_ctx = {"price": f"{twii_price:,.2f}", "change_pct": twii_pct}
        elif not twii_df.empty and "close" in twii_df.columns:
            latest_p = float(twii_df["close"].iloc[-1])
            prev_p = float(twii_df["close"].iloc[-2]) if len(twii_df) > 1 else latest_p
            market_ctx = {"price": f"{latest_p:,.0f}", "change_pct": (latest_p - prev_p) / prev_p * 100}

        with st.spinner("雷達分析中..."):
            reply = chat_with_radar(
                user_message=chat_input,
                chat_history=st.session_state.chat_history,
                portfolio=portfolio_ctx,
                news=st.session_state.get("news_data", []),
                market_data=market_ctx,
                capital=get_capital()
            )

        # 記錄對話
        st.session_state.chat_history.append({"role": "user", "content": chat_input})
        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        st.rerun()

# ╔══════════════════════════════╗
# ║  中欄：我的持股               ║
# ╚══════════════════════════════╝
with col_portfolio:
    st.markdown("## 📋 我的持股")

    if holdings.empty:
        st.info("尚未建立持股，請在下方新增")
    else:
        for _, row in holdings.iterrows():
            stock_id = row["stock_id"]
            stock_name = row.get("stock_name", stock_id) or stock_id
            buy_price = float(row["buy_price"])
            shares = float(row["shares"])       # 股數（統一單位）
            unit = row.get("unit", "股") or "股"
            cost = buy_price * shares

            # 顯示持有量（依原始單位）
            if unit == "張":
                lots = int(shares // 1000)
                rem = int(shares % 1000)
                qty_display = f"{lots}張" + (f" {rem}股" if rem else "")
            else:
                qty_display = f"{int(shares)}股"

            # 取最新價（快取5分鐘）
            cache_key = f"price_{stock_id}"
            if cache_key not in st.session_state or \
               (datetime.now() - st.session_state.get(f"{cache_key}_time", datetime.min)).seconds > 300:
                df = get_stock_price(stock_id, days=5)
                current_price = float(df["close"].iloc[-1]) if not df.empty and "close" in df.columns else buy_price
                st.session_state[cache_key] = current_price
                st.session_state[f"{cache_key}_time"] = datetime.now()
            else:
                current_price = st.session_state[cache_key]

            market_value = current_price * shares
            pnl = market_value - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            stop_loss_price = round(buy_price * 0.92, 1)

            if pnl_pct <= -8:
                status, status_label, card_cls = "🔴", f"停損！賣出價≈{stop_loss_price}", "alert-red"
            elif pnl_pct <= -4:
                status, status_label, card_cls = "🟡", f"注意 停損線{stop_loss_price}", "alert-yellow"
            else:
                status, status_label, card_cls = "🟢", "正常持有", ""

            pnl_color = "#FF4B4B" if pnl >= 0 else "#22C55E"  # 台股慣例：獲利紅、虧損綠

            st.markdown(f"""
            <div class='metric-card {card_cls}'>
                <div style='display:flex; justify-content:space-between; align-items:flex-start;'>
                    <div>
                        <b>{status} {stock_id}</b> {stock_name}<br/>
                        <span style='color:#888; font-size:0.78rem;'>
                            買入 {buy_price:.2f} × {qty_display}<br/>
                            成本 NT${cost:,.0f} ｜ 市值 NT${market_value:,.0f}
                        </span>
                    </div>
                    <div style='text-align:right;'>
                        <div style='font-size:1.05rem; font-weight:bold;'>{current_price:.2f}</div>
                        <div style='color:{pnl_color}; font-size:0.85rem;'>{pnl:+,.0f}（{pnl_pct:+.1f}%）</div>
                        <div style='font-size:0.72rem; color:#888;'>{status_label}</div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            # 賣出 + 移除按鈕
            b1, b2 = st.columns(2)
            with b1:
                if st.button(f"💰 賣出 {stock_id}", key=f"sell_btn_{row['id']}"):
                    st.session_state[f"sell_mode_{row['id']}"] = True
            with b2:
                if st.button(f"🗑 移除（錯誤修正）", key=f"remove_{row['id']}"):
                    remove_holding(row["id"])
                    st.rerun()

            # 賣出表單（展開）
            if st.session_state.get(f"sell_mode_{row['id']}", False):
                with st.form(f"sell_form_{row['id']}", clear_on_submit=True):
                    st.markdown(f"**賣出 {stock_id} {stock_name}**")
                    st.markdown(f"<div style='color:#888; font-size:0.82rem;'>持有 {qty_display} ｜ 買入均價 {buy_price:.2f} ｜ 最新收盤價 <b style='color:#00D4AA;'>{current_price:.2f}</b></div>",
                                unsafe_allow_html=True)
                    sc1, sc2, sc3 = st.columns(3)
                    with sc1:
                        # 預設帶入最新現價
                        sell_price = st.number_input("賣出價格（預設帶現價）", value=float(current_price), step=0.5, key=f"sp_{row['id']}")
                    with sc2:
                        sell_qty = st.number_input("賣出數量", min_value=0.001, value=1.0, step=1.0, key=f"sq_{row['id']}")
                    with sc3:
                        sell_unit = st.selectbox("單位", ["張", "股"], key=f"su_{row['id']}")
                    sell_date = st.date_input("賣出日期", value=datetime.today(), key=f"sd_{row['id']}")

                    # 預估損益
                    from utils.database import _to_shares
                    sell_s = _to_shares(sell_qty, sell_unit)
                    est_pnl = (sell_price - buy_price) * sell_s
                    est_color = "#FF4B4B" if est_pnl >= 0 else "#22C55E"  # 台股慣例
                    st.markdown(f"預估損益：<span style='color:{est_color}; font-weight:bold;'>NT$ {est_pnl:+,.0f}</span>　回收現金：NT$ {sell_price*sell_s:,.0f}",
                                unsafe_allow_html=True)

                    c_confirm, c_cancel = st.columns(2)
                    with c_confirm:
                        if st.form_submit_button("✅ 確認賣出"):
                            result = sell_holding(row["id"], sell_price, sell_qty, sell_unit, str(sell_date))
                            if "error" in result:
                                st.error(result["error"])
                            else:
                                pnl_txt = f"{'獲利' if result['pnl']>=0 else '虧損'} NT${abs(result['pnl']):,.0f}"
                                st.success(f"✅ {stock_id} 賣出完成！{pnl_txt}，回收 NT${result['proceeds']:,.0f}")
                                st.session_state.pop(f"sell_mode_{row['id']}", None)
                                for k in list(st.session_state.keys()):
                                    if k.startswith("price_"): del st.session_state[k]
                                st.rerun()
                    with c_cancel:
                        if st.form_submit_button("取消"):
                            st.session_state.pop(f"sell_mode_{row['id']}", None)
                            st.rerun()

    # 新增持股
    st.markdown("---")
    st.markdown("### ➕ 新增持股")

    # 股票代號輸入（自動查詢名稱+現價）
    lookup_id = st.text_input("股票代號", placeholder="輸入代號如 2330，自動帶入名稱與現價",
                               key="new_stock_id_lookup")

    auto_name = ""
    auto_price = 0.0

    if lookup_id.strip():
        sid_clean = lookup_id.strip()
        auto_name = get_dynamic_stock_name(sid_clean)
        ck = f"lookup_price_{sid_clean}"
        if ck not in st.session_state:
            with st.spinner(f"查詢 {sid_clean} 現價..."):
                auto_price = get_stock_current_price(sid_clean)
                st.session_state[ck] = auto_price
        else:
            auto_price = st.session_state[ck]

        # 代號改變時，重設均價 key 讓 number_input 讀到新現價
        last_sid = st.session_state.get("_last_lookup_id", "")
        if sid_clean != last_sid and auto_price > 0:
            st.session_state["add_price"] = float(auto_price)
            st.session_state["_last_lookup_id"] = sid_clean

        if auto_name or auto_price > 0:
            p_color = "#00D4AA"
            st.markdown(f"<div style='background:#1A1D27; border-left:3px solid {p_color}; padding:8px 12px; border-radius:6px; margin-bottom:8px;'>"
                        f"📌 <b>{sid_clean}</b> {auto_name or '（名稱未知）'} &nbsp;｜&nbsp; "
                        f"最新收盤價：<b style='color:{p_color};'>NT$ {auto_price:,.2f}</b></div>",
                        unsafe_allow_html=True)

    # 使用普通 widgets（非 form），讓計算即時更新
    fc1, fc2 = st.columns(2)
    with fc1:
        new_qty = st.number_input("買入數量", min_value=0.001, value=1.0, step=1.0, key="add_qty")
    with fc2:
        new_unit = st.selectbox("單位", ["張", "股（零股）"], key="add_unit")

    new_price = st.number_input(
        "買入均價（元）",
        min_value=0.01,
        step=0.5, format="%.2f", key="add_price"
    )
    new_date = st.date_input("買入日期", value=datetime.today(), key="add_date")
    new_note = st.text_input("備註（可略）", key="add_note")

    # 即時試算（每次數值變動自動更新）
    real_unit = "張" if new_unit == "張" else "股"
    from utils.database import _to_shares
    calc_shares = _to_shares(new_qty, real_unit)
    calc_cost = new_price * calc_shares
    unit_label = f"{new_qty:.0f}張（={calc_shares:,.0f}股）" if real_unit == "張" else f"{calc_shares:.0f}股"

    # 醒目顯示總金額
    _avail = float(capital.get("available_cash", 0))
    cost_color = "#FF4B4B" if calc_cost > _avail else "#00D4AA"
    st.markdown(f"""
    <div style='background:#1A1D27; border-radius:8px; padding:10px 14px; margin:6px 0; border-left:4px solid {cost_color};'>
        <span style='color:#888;'>{unit_label} × NT${new_price:.2f}</span>
        <span style='font-size:1.1rem; font-weight:bold; color:{cost_color}; float:right;'>
            總金額：NT$ {calc_cost:,.0f}
        </span>
        {"<div style='color:#FF4B4B; font-size:0.78rem;'>⚠️ 超過可用現金 NT$"+f"{_avail:,.0f}"+"</div>" if calc_cost > _avail else ""}
    </div>
    """, unsafe_allow_html=True)

    if st.button("✅ 確認買入", key="add_confirm"):
        if lookup_id.strip() and new_price > 0 and new_qty > 0:
            final_name = auto_name or lookup_id.strip()
            add_holding(lookup_id.strip(), final_name, new_price, new_qty, real_unit, str(new_date), new_note or "")
            for k in list(st.session_state.keys()):
                if k.startswith("price_") or k.startswith("lookup_price_"):
                    del st.session_state[k]
            st.success(f"✅ 買入 {lookup_id.strip()} {final_name}，扣除現金 NT${calc_cost:,.0f}")
            st.rerun()
        else:
            st.error("請先輸入股票代號")

    # 交易紀錄
    with st.expander("📋 交易歷史紀錄"):
        hist = get_trade_history()
        if hist.empty:
            st.write("尚無交易紀錄")
        else:
            for _, h in hist.iterrows():
                action_color = "#00D4AA" if h["action"] == "BUY" else "#FF4B4B"
                pnl_txt = f"　損益 NT${float(h.get('pnl',0)):+,.0f}" if h["action"] == "SELL" else ""
                st.markdown(f"<span style='color:{action_color};'>{'買入' if h['action']=='BUY' else '賣出'}</span> "
                            f"**{h['stock_id']}** @ {float(h['price']):.2f} × {float(h['shares']):.0f}股 "
                            f"（{h.get('trade_date','')}）{pnl_txt}",
                            unsafe_allow_html=True)

# ╔══════════════════════════════╗
# ║  右欄：資金生死表             ║
# ╚══════════════════════════════╝
with col_capital:
    st.markdown("## 💰 資金生死表")

    available = float(capital["available_cash"])
    invested = float(capital["total_invested"])

    # 計算總市值與總損益
    total_market_value = 0.0
    total_cost = 0.0
    if not holdings.empty:
        for _, row in holdings.iterrows():
            buy_price = float(row["buy_price"])
            shares = int(row["shares"])
            sid = row["stock_id"]
            ck = f"price_{sid}"
            current = st.session_state.get(ck, buy_price)
            total_market_value += current * shares
            total_cost += buy_price * shares

    total_assets = available + total_market_value
    total_pnl = total_market_value - total_cost
    pnl_pct_total = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    # 生命線判斷
    lifeline_pct = (total_assets / ORIGINAL_CAPITAL) * 100
    is_danger = total_assets < LIFELINE

    if is_danger:
        st.markdown("""
        <div class='metric-card alert-red' style='text-align:center;'>
            <div style='font-size:1.2rem; color:#FF4B4B; font-weight:bold;'>
                🚨 生命線突破！
            </div>
            <div style='color:#FF4B4B;'>資產低於原始資金30%<br/>強制停止操作</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        bar_color = "#00D4AA" if lifeline_pct >= 80 else "#FFD700" if lifeline_pct >= 50 else "#FF8C00"
        st.markdown(f"""
        <div class='metric-card'>
            <div style='color:#888; font-size:0.8rem;'>資產健康度</div>
            <div style='background:#2A2D3A; border-radius:5px; height:8px; margin:6px 0;'>
                <div style='background:{bar_color}; width:{min(lifeline_pct,100):.0f}%; height:8px; border-radius:5px;'></div>
            </div>
            <div style='color:{bar_color};'>{lifeline_pct:.1f}% &nbsp; 生命線：30%</div>
        </div>
        """, unsafe_allow_html=True)

    # 資金數字
    realized_pnl = float(capital.get("realized_pnl", 0))

    metrics = [
        ("原始資金", f"NT$ {ORIGINAL_CAPITAL:,.0f}", "#888"),
        ("生命線 (30%)", f"NT$ {LIFELINE:,.0f}", "#FF4B4B"),
        ("── 現金 ──", "", "#444"),
        ("可用現金", f"NT$ {available:,.0f}", "#00D4AA"),
        ("── 持股 ──", "", "#444"),
        ("持股成本", f"NT$ {total_cost:,.0f}", "#FAFAFA"),
        ("持股市值", f"NT$ {total_market_value:,.0f}", "#FAFAFA"),
        ("未實現損益", f"NT$ {total_pnl:+,.0f} ({pnl_pct_total:+.1f}%)",
         "#FF4B4B" if total_pnl >= 0 else "#22C55E"),   # 台股慣例：獲利紅
        ("── 已實現 ──", "", "#444"),
        ("已實現損益", f"NT$ {realized_pnl:+,.0f}",
         "#FF4B4B" if realized_pnl >= 0 else "#22C55E"),  # 台股慣例：獲利紅
        ("── 合計 ──", "", "#444"),
        ("總資產", f"NT$ {total_assets:,.0f}",
         "#FF4B4B" if total_assets >= ORIGINAL_CAPITAL else "#FF8C00"),
        ("總損益", f"NT$ {(total_pnl+realized_pnl):+,.0f}",
         "#FF4B4B" if (total_pnl+realized_pnl) >= 0 else "#22C55E"),  # 台股慣例：獲利紅
    ]

    for label, value, color in metrics:
        if label.startswith("──"):
            st.markdown(f"<div style='color:#444; font-size:0.7rem; padding:3px 0; border-bottom:1px solid #1A1D27;'>{label}</div>", unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div style='display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #2A2D3A;'>
                <span style='color:#888; font-size:0.83rem;'>{label}</span>
                <span style='color:{color}; font-weight:bold; font-size:0.83rem;'>{value}</span>
            </div>
            """, unsafe_allow_html=True)

    # ── 最近交易明細 ─────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📒 最近交易明細")
    hist = get_trade_history()
    if hist.empty:
        st.markdown("<div style='color:#888; font-size:0.82rem;'>尚無交易紀錄</div>", unsafe_allow_html=True)
    else:
        for _, h in hist.head(8).iterrows():
            action = h["action"]
            price = float(h["price"])
            shares = float(h["shares"])
            pnl = float(h.get("pnl") or 0)
            sid = h["stock_id"]
            sname = h.get("stock_name", sid) or sid
            date = h.get("trade_date", "")

            if action == "BUY":
                cost = price * shares
                st.markdown(f"""
                <div style='padding:5px 8px; border-left:3px solid #00D4AA; background:#1A1D27; border-radius:4px; margin-bottom:4px; font-size:0.8rem;'>
                    <b style='color:#00D4AA;'>買入</b> {sid} {sname} &nbsp;
                    {price:.2f} × {shares:.0f}股 &nbsp;
                    <span style='color:#FF4B4B;'>−NT${cost:,.0f}</span>
                    <span style='color:#444; float:right;'>{date}</span>
                </div>
                """, unsafe_allow_html=True)
            else:
                proceeds = price * shares
                pnl_color = "#FF4B4B" if pnl >= 0 else "#22C55E"  # 台股慣例：獲利紅、虧損綠
                pnl_label = f"獲利 +NT${pnl:,.0f}" if pnl >= 0 else f"虧損 NT${pnl:,.0f}"
                st.markdown(f"""
                <div style='padding:5px 8px; border-left:3px solid {"#FF4B4B" if pnl>=0 else "#22C55E"}; background:#1A1D27; border-radius:4px; margin-bottom:4px; font-size:0.8rem;'>
                    <b style='color:#888;'>賣出</b> {sid} {sname} &nbsp;
                    {price:.2f} × {shares:.0f}股 &nbsp;
                    <span style='color:#00D4AA;'>+NT${proceeds:,.0f}</span> &nbsp;
                    <span style='color:{pnl_color}; font-weight:bold;'>（{pnl_label}）</span>
                    <span style='color:#444; float:right;'>{date}</span>
                </div>
                """, unsafe_allow_html=True)

    # ── 溫馨提醒視窗 ────────────────────────────────────
    st.markdown("---")
    st.markdown("## 💌 溫馨提醒")

    # 從排程器寫入的 alerts 取出提醒類型
    all_alerts = get_unread_alerts()
    reminders = all_alerts[all_alerts["alert_type"].isin(
        ["morning_reminder", "stop_loss", "take_profit", "technical_weak",
         "technical_strong", "prophet", "news_impact", "watch"]
    )] if not all_alerts.empty else pd.DataFrame()

    # 持股即時風險（即時計算，不等排程）
    live_warnings = []
    if not holdings.empty:
        for _, row in holdings.iterrows():
            sid = row["stock_id"]
            buy = float(row["buy_price"])
            cur = st.session_state.get(f"price_{sid}", buy)
            pnl = (cur - buy) / buy * 100
            if pnl <= -8:
                live_warnings.append(("danger", f"🔴 **{sid}** 跌幅 {pnl:.1f}%，已觸停損線！立即考慮賣出"))
            elif pnl <= -4:
                live_warnings.append(("warning", f"🟡 **{sid}** 跌幅 {pnl:.1f}%，須注意"))
            elif pnl >= 10:
                live_warnings.append(("profit", f"💰 **{sid}** 漲幅 {pnl:.1f}%，可考慮部分停利"))

    # 資金生命線警告
    if total_assets < LIFELINE * 1.3:
        live_warnings.append(("danger",
            f"🚨 資金警告：總資產 NT${total_assets:,.0f} 距生命線 NT${LIFELINE:,.0f} 僅剩 {((total_assets-LIFELINE)/LIFELINE*100):.0f}%！"))

    # 顯示即時警告
    if live_warnings:
        for wtype, wmsg in live_warnings:
            color = "#FF4B4B" if wtype == "danger" else "#FFD700" if wtype == "warning" else "#00D4AA"
            st.markdown(f"<div style='padding:8px 12px; border-left:3px solid {color}; background:#1A1D27; border-radius:6px; margin-bottom:6px; font-size:0.85rem;'>{wmsg}</div>",
                        unsafe_allow_html=True)

    # 讀取最新先知分析摘要
    prophet_file = os.path.join(os.path.dirname(__file__), "data", "prophet_latest.json")
    if os.path.exists(prophet_file):
        try:
            with open(prophet_file, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            ts = pdata.get("timestamp", "")
            summary = pdata.get("analysis", "")[:200]
            st.markdown(f"""
            <div style='padding:10px 12px; border-left:3px solid #00D4AA; background:#1A1D27; border-radius:6px; margin-bottom:6px;'>
                <div style='color:#00D4AA; font-size:0.75rem; margin-bottom:4px;'>🔮 先知分析摘要 ({ts})</div>
                <div style='font-size:0.82rem; color:#FAFAFA;'>{summary}...</div>
            </div>
            """, unsafe_allow_html=True)
        except Exception:
            pass

    # 無提醒時顯示
    if not live_warnings and not os.path.exists(prophet_file):
        st.markdown("<div style='color:#888; font-size:0.82rem;'>目前無特別提醒。排程器啟動後，每日8:30 自動生成晨間建議。</div>",
                    unsafe_allow_html=True)

    # 更新現金
    st.markdown("---")
    st.markdown("### ⚙️ 更新可用現金")
    with st.form("update_cash"):
        new_cash = st.number_input("可用現金（NT$）", value=available, step=1000.0)
        if st.form_submit_button("更新"):
            update_available_cash(new_cash)
            st.success("已更新")
            st.rerun()

    # 先知分析（精簡版）
    st.markdown("---")
    st.markdown("## 🔮 先知分析")

    if st.button("🔮 啟動先知分析", key="prophet_btn"):
        with st.spinner("步驟1/3：自動更新新聞..."):
            # 自動更新新聞（不需手動）
            if not st.session_state.get("news_data"):
                st.session_state["news_data"] = get_all_news(max_per_source=8)
                st.session_state["news_time"] = datetime.now()

        with st.spinner("步驟2/3：偵測主力攻擊..."):
            attack_data = detect_attacks()

        with st.spinner("步驟3/3：Claude 先知整合分析（30-60秒）..."):
            news_for_prophet = st.session_state.get("news_data", [])
            twii_ctx = ""
            if twii_price:
                twii_ctx = f"大盤即時 {twii_price:,.2f} 點，今日 {twii_pct:+.2f}%"
            elif not twii_df.empty and "close" in twii_df.columns:
                latest_price = float(twii_df["close"].iloc[-1])
                prev_price = float(twii_df["close"].iloc[-5]) if len(twii_df) > 5 else latest_price
                twii_ctx = f"大盤 {latest_price:,.0f} 點，近5日 {((latest_price-prev_price)/prev_price*100):+.1f}%"

            # 重試邏輯（處理 529 過載）
            result = None
            for attempt in range(3):
                result = get_prophet_analysis(news_for_prophet, twii_ctx, attack_data)
                if result.get("status") == "ok":
                    break
                if "過載" in result.get("analysis", "") or "overload" in result.get("analysis", "").lower():
                    import time
                    time.sleep(5 * (attempt + 1))  # 5s, 10s, 15s
                else:
                    break

            st.session_state["prophet_result"] = result.get("analysis", "")
            st.session_state["prophet_meta"] = {
                "news_count": result.get("news_count", 0),
                "timestamp": result.get("timestamp", ""),
                "status": result.get("status", ""),
                "attack_date": attack_data.get("date", "")
            }

    if "prophet_result" in st.session_state and st.session_state["prophet_result"]:
        meta = st.session_state.get("prophet_meta", {})
        if meta.get("news_count") or meta.get("attack_date"):
            st.markdown(f"<div style='color:#888; font-size:0.72rem; margin-bottom:6px;'>"
                        f"新聞 {meta.get('news_count','?')} 條 ｜ 法人資料 {meta.get('attack_date','')} ｜ {meta.get('timestamp','')}</div>",
                        unsafe_allow_html=True)
        st.markdown(f"""
        <div class='prophet-box' style='font-size:0.85rem; white-space:pre-wrap;'>
{st.session_state["prophet_result"]}
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style='color:#888; font-size:0.85rem; line-height:1.6;'>
        點「🔮 啟動先知分析」<br/>
        系統將自動讀取目前新聞牆的即時新聞<br/>
        由 Claude AI 深度解讀對台股的影響
        </div>
        """, unsafe_allow_html=True)

# ╔══════════════════════════════════════════════════════╗
# ║  推薦股 × 堪憂股（雙欄）                               ║
# ╚══════════════════════════════════════════════════════╝
st.divider()
rec_col, warn_col = st.columns(2)

# ── 推薦股 ──────────────────────────────────────────────
with rec_col:
    st.markdown("## 📈 推薦股清單")
    st.markdown("""
    <div style='font-size:0.75rem; margin-bottom:8px; line-height:1.8;'>
        📊 <b>排序邏輯</b>：從監控清單掃描所有股票，依五層總分由高到低排列，顯示前15名。<br/>
        <b>這就是當日最值得關注的股票順序</b>，#1最高分、越前面越優先考慮。<br/>
        <span style='color:#FF3333; font-weight:bold;'>■ 紅色（≥70分）= 強烈推薦</span> &nbsp;
        <span style='color:#FFD700;'>■ 黃色（50-69分）= 值得關注</span> &nbsp;
        <span style='color:#FF8C00;'>■ 橘色（&lt;50分）= 參考觀察</span><br/>
        <span style='color:#888;'>台股慣例：紅漲綠跌｜滿分100分（五層各20）｜每日14:10更新｜非投資建議</span>
    </div>""", unsafe_allow_html=True)

    if st.button("🔄 更新推薦股", key="refresh_rec"):
        if "rec_stocks" in st.session_state:
            del st.session_state["rec_stocks"]

    # 快取（每日更新一次）
    if "rec_stocks" not in st.session_state:
        with st.spinner("掃描25支股票中（需30-60秒）..."):
            atk = st.session_state.get("atk_cache") or detect_attacks()
            st.session_state["atk_cache"] = atk
            st.session_state["rec_stocks"] = get_recommended_stocks(
                top_n=15, attack_data=atk,
                news_list=st.session_state.get("news_data", [])
            )

    rec_stocks = st.session_state.get("rec_stocks", [])
    if not rec_stocks:
        st.info("資料不足，請點「更新推薦股」重試")
    else:
        for stock in rec_stocks:
            score = stock["total_score"]
            # 台股慣例：紅=強烈推薦（漲），黃=關注，橘=參考（分母100分）
            score_color = "#FF3333" if score >= 70 else "#FFD700" if score >= 50 else "#FF8C00"
            reasons_txt = " · ".join(stock.get("reasons", []))
            st.markdown(f"""
            <div style='background:#1A1D27; border-radius:8px; padding:10px 12px; margin-bottom:6px;
                        border-left:4px solid {score_color};'>
                <div style='display:flex; justify-content:space-between; align-items:center;'>
                    <div>
                        <span style='font-weight:bold; color:{score_color};'>#{stock["rank"]} {stock["stock_id"]}</span>
                        <span style='color:#FAFAFA;'> {stock["stock_name"]}</span>
                        <span style='color:#888; font-size:0.78rem;'> · NT${stock["current_price"]:.0f}</span>
                    </div>
                    <div style='text-align:right;'>
                        <span style='color:{score_color}; font-weight:bold; font-size:1rem;'>{score}/100</span>
                        <span style='color:#888; font-size:0.72rem;'>分</span>
                    </div>
                </div>
                <div style='color:#888; font-size:0.75rem; margin-top:4px;'>
                    技術{stock["tech_score"]} 籌碼{stock["chip_score"]} 基本{stock.get("fund_score",0)} 產業{stock.get("sect_score",0)} 先知{stock.get("proph_score",0)} ｜ RSI {stock.get("rsi",0):.0f}
                </div>
                <div style='color:#00D4AA; font-size:0.75rem;'>{reasons_txt}</div>
            </div>
            """, unsafe_allow_html=True)

# ── 堪憂股 ──────────────────────────────────────────────
with warn_col:
    st.markdown("## ⚠️ 堪憂股清單")
    st.markdown("""
    <div style='font-size:0.75rem; margin-bottom:8px; line-height:1.8;'>
        📊 <b>顯示邏輯</b>：持股有風險 優先顯示；監控清單出現技術崩壞/外資大賣 次之。<br/>
        <b>目前無警示 = 台股多頭行情中，尚無明顯危險訊號</b>（空頭來臨時會自動跳出）。<br/>
        <span style='color:#22C55E; font-weight:bold;'>■ 綠色 = 持股停損警告（跌逾-8%，應立即賣出）</span><br/>
        <span style='color:#A855F7;'>■ 紫色 = 持股轉弱，考慮減碼</span>
        <span style='color:#FFD700;'>■ 黃色 = 監控清單風險訊號，尚未持有勿進場</span><br/>
        <span style='color:#888;'>台股慣例：紅漲綠跌｜持股優先｜非投資建議</span>
    </div>""", unsafe_allow_html=True)

    if st.button("🔄 更新堪憂股", key="refresh_warn"):
        if "warn_stocks" in st.session_state:
            del st.session_state["warn_stocks"]

    if "warn_stocks" not in st.session_state:
        with st.spinner("掃描風險中..."):
            atk_w = st.session_state.get("atk_cache") or detect_attacks()
            st.session_state["warn_stocks"] = get_warning_stocks(
                holdings, attack_data=atk_w,
                news_list=st.session_state.get("news_data", [])
            )

    warn_stocks = st.session_state.get("warn_stocks", [])
    if not warn_stocks:
        st.success("✅ 目前無堪憂警示")
    else:
        for stock in warn_stocks:
            is_holding = stock.get("is_holding", False)
            action = stock.get("action", "觀察")
            urgency = stock.get("urgency", 0)

            # 台股慣例：綠=跌（停損）、紫=轉弱、黃=風險
            action_color = "#22C55E" if (is_holding and urgency >= 8) else "#A855F7" if is_holding else "#FFD700"
            border_color = "#22C55E" if (is_holding and urgency >= 8) else "#A855F7" if is_holding else "#FFD700"

            holding_tag = "🔴 持股" if is_holding else "👁 監控"
            pnl_color = "#FF4B4B" if (stock.get("pnl_pct") or 0) < 0 else "#00D4AA"
            pnl_str = f"  損益 {stock['pnl_pct']:+.1f}%" if (is_holding and stock.get("pnl_pct") is not None) else ""

            # 用 st.container 避免 HTML 引號衝突
            with st.container():
                st.markdown(f"""
                <div style='background:#1A1D27; border-radius:8px; padding:10px 12px; margin-bottom:2px;
                            border-left:4px solid {border_color};'>
                    <div style='display:flex; justify-content:space-between; align-items:center;'>
                        <div>
                            <span style='color:{border_color}; font-weight:bold;'>{holding_tag} {stock["stock_id"]}</span>
                            <span style='color:#FAFAFA;'> {stock["stock_name"]}</span>
                            <span style='color:#888; font-size:0.78rem;'> NT${stock["current_price"]:.0f}</span>
                            <span style='color:{pnl_color}; font-size:0.78rem;'>{pnl_str}</span>
                        </div>
                        <span style='color:{action_color}; font-weight:bold; font-size:0.85rem;'>{action}</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                # 風險原因用 st.markdown 純文字避免 HTML 問題
                for reason in stock.get("risk_reasons", []):
                    st.markdown(f"<div style='color:#FF8C00; font-size:0.78rem; padding:1px 14px;'>• {reason}</div>",
                                unsafe_allow_html=True)
                st.markdown("<div style='margin-bottom:6px;'></div>", unsafe_allow_html=True)

# ╔══════════════════════════════════════════════════════╗
# ║  每日重點（全寬）                                      ║
# ╚══════════════════════════════════════════════════════╝
st.divider()
st.markdown("## 💡 每日操作重點")
st.markdown("""
<div style='background:#0D1117; border:1px solid #2A2D3A; border-radius:10px; padding:14px 18px; margin-bottom:12px;'>
<span style='color:#888; font-size:0.78rem;'>
系統依五層100分評分 + 籌碼異常偵測，自動整理今日最值得行動的結論。<br/>
<b>評分≥70 → 強烈推薦。評分50-69 → 觀察等待。持股觸停損-8% → 立即賣出。</b><br/>
所有建議均基於真實數據，非幻想捏造。最終買賣決策由您做主。
</span>
</div>
""", unsafe_allow_html=True)

_daily_rec = st.session_state.get("rec_stocks", [])
_daily_warn = st.session_state.get("warn_stocks", [])

# 取出強烈推薦（≥70分）最多3支
_buy_list = [s for s in _daily_rec if s["total_score"] >= 70][:3]
# 取出70以下但≥55，作為觀察候選
_watch_list = [s for s in _daily_rec if 55 <= s["total_score"] < 70][:2]
# 持股停損警告（urgency≥8）
_sell_list = [s for s in _daily_warn if s.get("is_holding") and s.get("urgency", 0) >= 8]

daily_c1, daily_c2 = st.columns([1, 1])

with daily_c1:
    # ── 今日建議買入 ─────────────────────────────────────
    if _buy_list:
        st.markdown("### 📌 今日建議買入")
        for s in _buy_list:
            reasons_txt = "、".join(s.get("reasons", [])[:2]) or "五層評分均佳"
            st.markdown(f"""
            <div style='background:#0A1F0A; border:1px solid #22C55E; border-radius:8px;
                        padding:12px 14px; margin-bottom:8px;'>
                <div style='display:flex; justify-content:space-between; align-items:center;'>
                    <div>
                        <span style='color:#22C55E; font-weight:bold; font-size:1.05rem;'>
                            #{s["rank"]} {s["stock_id"]} {s["stock_name"]}
                        </span>
                        <span style='color:#888; font-size:0.8rem;'> · NT${s["current_price"]:.0f}</span>
                    </div>
                    <span style='color:#22C55E; font-weight:bold; font-size:1.1rem;'>{s["total_score"]}/100</span>
                </div>
                <div style='color:#22C55E; font-size:0.82rem; margin-top:6px; font-weight:bold;'>
                    ✅ 建議理由：{reasons_txt}
                </div>
                <div style='color:#888; font-size:0.75rem; margin-top:4px;'>
                    技術{s["tech_score"]} 籌碼{s["chip_score"]} 基本{s.get("fund_score",0)}
                    產業{s.get("sect_score",0)} 先知{s.get("proph_score",0)}
                </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown("### 📌 今日建議買入")
        st.markdown("""
        <div style='background:#1A1D27; border:1px solid #2A2D3A; border-radius:8px;
                    padding:12px 14px; color:#888; font-size:0.88rem;'>
            ⏸ 今日無強烈推薦（評分均未達70分）<br/>
            <span style='font-size:0.78rem;'>台股整體動能不足，建議觀察等待更佳進場時機。</span>
        </div>
        """, unsafe_allow_html=True)

    if _watch_list:
        st.markdown("##### 👁 可繼續觀察（評分55-69）")
        for s in _watch_list:
            st.markdown(f"""
            <div style='background:#1A1D27; border-left:3px solid #FFD700; border-radius:6px;
                        padding:8px 12px; margin-bottom:6px; font-size:0.85rem;'>
                <span style='color:#FFD700; font-weight:bold;'>{s["stock_id"]} {s["stock_name"]}</span>
                <span style='color:#888;'> · {s["total_score"]}/100 · NT${s["current_price"]:.0f}</span><br/>
                <span style='color:#888; font-size:0.75rem;'>{'、'.join(s.get("reasons", [])[:2]) or "評分中等，持續觀察"}</span>
            </div>
            """, unsafe_allow_html=True)

with daily_c2:
    # ── 今日建議賣出 ─────────────────────────────────────
    st.markdown("### 🚨 今日建議賣出")
    if _sell_list:
        for s in _sell_list:
            pnl_pct = s.get("pnl_pct") or 0
            reasons_txt = "、".join(s.get("risk_reasons", [])[:2]) or "觸及停損線"
            st.markdown(f"""
            <div style='background:#1F0A0A; border:1px solid #FF3333; border-radius:8px;
                        padding:12px 14px; margin-bottom:8px;'>
                <div style='display:flex; justify-content:space-between; align-items:center;'>
                    <div>
                        <span style='color:#FF3333; font-weight:bold; font-size:1.05rem;'>
                            {s["stock_id"]} {s["stock_name"]}
                        </span>
                        <span style='color:#888; font-size:0.8rem;'> · NT${s["current_price"]:.0f}</span>
                    </div>
                    <span style='color:#FF3333; font-weight:bold; font-size:1.1rem;'>{pnl_pct:+.1f}%</span>
                </div>
                <div style='color:#FF3333; font-size:0.82rem; margin-top:6px; font-weight:bold;'>
                    ❌ 建議賣出：{reasons_txt}
                </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style='background:#0A1F0A; border:1px solid #22C55E; border-radius:8px;
                    padding:12px 14px; color:#22C55E; font-size:0.88rem;'>
            ✅ 今日持股無需賣出<br/>
            <span style='color:#888; font-size:0.78rem;'>
            所有持股均未觸及停損（-8%）。繼續持有，等待獲利訊號。
            </span>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)
    st.markdown("""
    <div style='background:#1A1D27; border-radius:8px; padding:10px 14px; font-size:0.75rem; color:#555;'>
    📋 <b>操作流程提醒</b><br/>
    1. 建議買入 → 確認自己資金夠 → 不超過單筆15%倉位<br/>
    2. 建議賣出 → 停損優先，不猶豫<br/>
    3. 觀察等待 → 不追高、不搶進，等評分升至70再考慮<br/>
    4. 每日20:00自動更新，明早看最新建議
    </div>
    """, unsafe_allow_html=True)

if not _daily_rec:
    st.info("📌 推薦股清單尚未載入，請先點上方「更新推薦股」，再重新整理頁面即可看到今日重點。")

# ╔══════════════════════════════════════════════════════╗
# ║  即時新聞牆（全寬）                                    ║
# ╚══════════════════════════════════════════════════════╝
st.divider()
st.markdown("## 📰 即時財經新聞")

news_col1, news_col2 = st.columns([1, 4])
with news_col1:
    st.markdown("<div style='padding-top:6px;'></div>", unsafe_allow_html=True)
    refresh_news = st.button("🔄 更新新聞", key="refresh_news")
    show_impact_only = st.checkbox("只看重要新聞", key="impact_only")

# 快取新聞（5分鐘）
if refresh_news or "news_data" not in st.session_state or \
   (datetime.now() - st.session_state.get("news_time", datetime.min)).seconds > 300:
    with st.spinner("載入新聞..."):
        st.session_state["news_data"] = get_all_news(max_per_source=8)
        st.session_state["news_time"] = datetime.now()

news_list = st.session_state.get("news_data", [])
if show_impact_only:
    news_list = [n for n in news_list if n["high_impact"]]

if not news_list:
    st.info("⚠️ 暫無新聞資料（可能是網路問題），點「更新新聞」重試")
else:
    # 分三欄顯示
    nc1, nc2, nc3 = st.columns(3)
    cols = [nc1, nc2, nc3]
    for i, news in enumerate(news_list[:30]):
        col = cols[i % 3]
        with col:
            impact_tag = "🔴 " if news["high_impact"] else ""
            source_color = "#00D4AA" if news["lang"] == "zh" else "#888"
            st.markdown(f"""
            <div style='background:#1A1D27; border-radius:8px; padding:10px 12px; margin-bottom:8px;
                        border-left:3px solid {"#FF4B4B" if news["high_impact"] else "#2A2D3A"};'>
                <div style='font-size:0.75rem; color:{source_color}; margin-bottom:4px;'>
                    {impact_tag}{news["source"]} &nbsp;·&nbsp; {news.get("time","")}
                </div>
                <a href='{news["link"]}' target='_blank' style='color:#FAFAFA; text-decoration:none; font-size:0.88rem; font-weight:bold; line-height:1.4;'>
                    {news["title"]}
                </a>
            </div>
            """, unsafe_allow_html=True)

st.markdown(f"<div style='color:#444; font-size:0.72rem; margin-top:4px;'>最後更新：{st.session_state.get('news_time', datetime.now()).strftime('%H:%M:%S')} ｜ 來源：鉅亨網、Yahoo Finance、Reuters</div>", unsafe_allow_html=True)

# ── 底部刷新 ────────────────────────────────────────────
st.divider()
col_r1, col_r2, col_r3 = st.columns([1, 1, 2])
with col_r1:
    if st.button("🔄 刷新數據"):
        # 清除價格快取
        for key in list(st.session_state.keys()):
            if key.startswith("price_"):
                del st.session_state[key]
        st.rerun()
with col_r2:
    st.markdown(f"<div style='color:#444; font-size:0.75rem; padding-top:8px;'>資料來源：FinMind・Claude API</div>",
                unsafe_allow_html=True)
