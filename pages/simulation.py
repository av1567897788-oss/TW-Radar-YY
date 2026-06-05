"""
TW-Radar 模擬驗證頁面
虛擬投資客績效追蹤 + 系統精準度報告
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.virtual_investors import (
    INVESTORS, init_sim_db, get_sim_capital, get_sim_holdings,
    get_all_trades, get_daily_reports, get_accuracy_stats
)

st.set_page_config(page_title="模擬驗證 | TW-Radar", page_icon="📊", layout="wide")

# 密碼驗證
if "authenticated" not in st.session_state or not st.session_state.authenticated:
    pwd = st.text_input("密碼", type="password")
    if st.button("登入"):
        try:
            correct = st.secrets.get("auth", {}).get("password", "changeme")
            if pwd == correct:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        except Exception:
            st.session_state.authenticated = True
            st.rerun()
    st.stop()

init_sim_db()

st.markdown("# 📊 虛擬投資客模擬驗證")
st.markdown(f"<div style='color:#888;'>驗證期間：2026/06/02 - 2026/07/01 ｜ 每日18:00更新</div>",
            unsafe_allow_html=True)
st.divider()

# ── 三位投資客資金總覽 ──────────────────────────────────
st.markdown("## 👥 三位投資客資金狀況")
cols = st.columns(3)

investor_data = {}
for i, (inv_id, inv) in enumerate(INVESTORS.items()):
    cap = get_sim_capital(inv_id)
    holdings = get_sim_holdings(inv_id)

    # 市值估算（用買入價代替，實際需現價）
    if not holdings.empty:
        market_val = (holdings["buy_price"].astype(float) * holdings["shares"].astype(float)).sum()
    else:
        market_val = 0

    total_assets = float(cap.get("cash", 0)) + market_val
    orig = inv["capital"]
    pnl = total_assets - orig
    pnl_pct = pnl / orig * 100

    investor_data[inv_id] = {
        "total": total_assets, "pnl": pnl, "pnl_pct": pnl_pct,
        "cash": cap.get("cash", 0), "realized": cap.get("realized_pnl", 0)
    }

    with cols[i]:
        color = "#FF4B4B" if pnl >= 0 else "#22C55E"  # 台股慣例：獲利紅、虧損綠
        st.markdown(f"""
        <div style='background:#1A1D27; border-radius:10px; padding:14px;
                    border-left:4px solid {color};'>
            <div style='font-size:1.1rem; font-weight:bold;'>{inv["emoji"]} {inv["name"]}</div>
            <div style='color:#888; font-size:0.78rem; margin-bottom:8px;'>{inv["title"]}</div>
            <div style='font-size:1.4rem; font-weight:bold; color:{color};'>
                NT${total_assets:,.0f}
            </div>
            <div style='color:{color}; font-size:0.9rem;'>{pnl:+,.0f}（{pnl_pct:+.1f}%）</div>
            <div style='color:#888; font-size:0.75rem; margin-top:4px;'>
                現金 NT${cap.get("cash",0):,.0f} ｜
                已實現 NT${cap.get("realized_pnl",0):+,.0f}
            </div>
        </div>
        """, unsafe_allow_html=True)

st.divider()

# ── 系統預測準確率 ──────────────────────────────────────
st.markdown("## 🎯 系統預測準確率")
acc = get_accuracy_stats()

ac1, ac2, ac3 = st.columns(3)
with ac1:
    st.metric("總預測筆數", acc["total"])
with ac2:
    st.metric("預測正確", acc["correct"])
with ac3:
    rate = acc["accuracy"]
    color = "normal" if rate >= 60 else "inverse"
    st.metric("整體準確率", f"{rate}%", delta=f"目標 ≥ 60%")

st.divider()

# ── 每日報告 ──────────────────────────────────────────
st.markdown("## 📋 今日操作報告")
tab1, tab2, tab3 = st.tabs([
    f"{INVESTORS['wang']['emoji']} {INVESTORS['wang']['name']}",
    f"{INVESTORS['lin']['emoji']} {INVESTORS['lin']['name']}",
    f"{INVESTORS['chen']['emoji']} {INVESTORS['chen']['name']}"
])

for tab, inv_id in zip([tab1, tab2, tab3], ["wang", "lin", "chen"]):
    inv = INVESTORS[inv_id]
    with tab:
        st.markdown(f"**{inv['personality']}**")
        st.markdown(f"進場門檻：{inv['strategy']['min_score']}/100 ｜ 停損：{inv['strategy']['stop_loss']*100:.0f}% ｜ 停利：{inv['strategy']['take_profit']*100:.0f}%")
        st.divider()

        reports = get_daily_reports(inv_id, limit=5)
        if reports.empty:
            st.info("尚無操作記錄，等待首次模擬執行")
        else:
            for _, rep in reports.iterrows():
                try:
                    data = json.loads(rep["report_json"])
                    date = rep["report_date"]
                    cap_d = data.get("capital", {})
                    actions = data.get("actions", [])

                    st.markdown(f"### {date}")
                    if actions:
                        for act in actions:
                            if act["type"] == "BUY":
                                st.markdown(f"🔴 **買入** {act['stock']} {act['name']} @{act['price']:.0f}元 "
                                           f"{act.get('shares',0)}股 ｜ 評分{act.get('score',0)}/100")
                            else:
                                pnl = act.get("pnl", 0)
                                c = "#FF3333" if pnl >= 0 else "#22C55E"
                                st.markdown(f"🟢 **賣出** {act['stock']} {act['name']} @{act['price']:.0f}元 "
                                           f"｜ {act.get('reason','')} "
                                           f"｜ 損益 <span style='color:{c};'>NT${pnl:+,.0f}</span>",
                                           unsafe_allow_html=True)
                    else:
                        st.info(f"今日無操作 — {data.get('no_action_reason','觀察等待')}")

                    st.markdown(f"總資產 NT${cap_d.get('total_assets',0):,.0f} "
                               f"（{cap_d.get('total_pnl_pct',0):+.1f}%）")
                    st.divider()
                except Exception:
                    pass

        # 交易歷史
        with st.expander("📋 完整交易歷史"):
            trades = get_all_trades(inv_id)
            if trades.empty:
                st.write("無交易記錄")
            else:
                for _, t in trades.iterrows():
                    pnl = float(t.get("pnl") or 0)
                    action_color = "#FF3333" if t["action"] == "BUY" else "#22C55E"
                    st.markdown(
                        f"<span style='color:{action_color};'>{'買入' if t['action']=='BUY' else '賣出'}</span> "
                        f"**{t['stock_id']}** {t['stock_name']} @{float(t['price']):.0f} "
                        f"× {float(t['shares']):.0f}股 "
                        f"{'｜損益 NT$'+f'{pnl:+,.0f}' if t['action']=='SELL' else ''} "
                        f"<span style='color:#444;'>({t['trade_date']})</span>",
                        unsafe_allow_html=True
                    )

st.divider()

# ── 手動觸發模擬 ──────────────────────────────────────
st.markdown("## ⚙️ 手動執行模擬")
if st.button("▶️ 立即執行今日模擬", key="run_sim"):
    with st.spinner("執行中（約60-90秒）..."):
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from sim_runner import run_daily_simulation
            run_daily_simulation()
            st.success("✅ 今日模擬完成，重新整理查看結果")
            st.rerun()
        except Exception as e:
            st.error(f"執行失敗：{e}")
