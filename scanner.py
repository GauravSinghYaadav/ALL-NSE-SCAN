import os
import sys
import logging
import smtplib
import mimetypes
import urllib.request
import urllib.parse
from datetime import datetime
from email.message import EmailMessage

import numpy as np
import pandas as pd
import talib

# ==============================================================================
# 1. LOGGING SETUP
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("NSE_Scanner")

# ==============================================================================
# 2. GLOBAL CONFIGURATION & MAPS
# ==============================================================================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "nse.scanner.app@gmail.com"
SENDER_PASSWORD = "wmkdozoyfprduqgx"
RECIPIENTS = ["yadav.gauravsingh@gmail.com"]
BCC_RECIPIENTS = ["dipti.gorwadia@gmail.com", "yadav.gauravsingh34@gmail.com", "akshay.tiwari@gmail.com"]

TELEGRAM_BOT_TOKEN = "8344354642:AAG_S7mavtiLP_yXPh4YM4u31QD5BBWJmuM"
TELEGRAM_CHAT_IDS = ["5332984891", "-1002622207173"]

BASE_PATH = os.path.dirname(os.path.abspath(__file__)) if "__file__" in locals() else os.getcwd()
OUTPUT_DIR = os.path.join(BASE_PATH, "Output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

TIMEFRAME_FOLDERS = {
    "15 Min": ("stock_data_15", "15 Min Scan"),
    "Hourly": ("stock_data_1H", "Hourly Scan"),
    "Daily": ("stock_data_D", "Daily Scan"),
    "Weekly": ("stock_data_W", "Weekly Scan"),
    "Monthly": ("stock_data_M", "Monthly Scan"),
}

# Higher-Timeframe (HTF) to Lower-Timeframe (LTF) Pairings
HTF_LTF_MAP = [
    ("Weekly", "Daily"),
    ("Monthly", "Weekly")
]

SAFE_COLS = [
    "Symbol", "Signal", "Trend", "State", "Setup", 
    "Divergence", "RSI", "Zone", "Confluence", "Bias", 
    "Probability", "TV_Link"
]

def make_tradingview_link(sym: str) -> str:
    return f"https://in.tradingview.com/chart/LqUZraZ9/?symbol=NSE%3A{sym}"

# ==============================================================================
# 3. TECHNICAL SCANNING DEFINITIONS (Core Mathematical Logic)
# ==============================================================================
def detect_macd_divergence(df, lookback=30):
    """
    Detects 4 types of MACD Divergences safely handling warm-up bars and NaNs:
    1. Bearish Divergence (ND): Price Higher High, MACD Lower High
    2. Bullish Divergence (ND): Price Lower Low, MACD Higher Low
    3. Reverse Bullish Divergence (RD): Price Higher Low, MACD Lower Low
    4. Reverse Bearish Divergence (RD): Price Lower High, MACD Higher High
    """
    if df is None or len(df) < 65:  # Enforce minimum bars for MACD 34-bar warm-up + 30 lookback
        return None

    try:
        close = pd.to_numeric(df["close"], errors="coerce")
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")

        macd, _, _ = talib.MACD(close, 12, 26, 9)

        # Ensure window slice contains valid numeric data
        if macd.iloc[-lookback:].isna().any():
            return None

        # Segment windows: Window 1 (Older 15 bars), Window 2 (Recent 15 bars)
        p_high1 = high.iloc[-lookback:-15].max()
        p_high2 = high.iloc[-15:].max()
        m_high1 = macd.iloc[-lookback:-15].max()
        m_high2 = macd.iloc[-15:].max()

        p_low1 = low.iloc[-lookback:-15].min()
        p_low2 = low.iloc[-15:].min()
        m_low1 = macd.iloc[-lookback:-15].min()
        m_low2 = macd.iloc[-15:].min()

        # 1. Bearish Normal Divergence (ND)
        if p_high2 > p_high1 and m_high2 < m_high1:
            return "Bearish ND"

        # 2. Bullish Normal Divergence (ND)
        if p_low2 < p_low1 and m_low2 > m_low1:
            return "Bullish ND"

        # 3. Reverse Bullish Divergence (RD) / Hidden Bullish
        if p_low2 > p_low1 and m_low2 < m_low1:
            return "Bullish RD"

        # 4. Reverse Bearish Divergence (RD) / Hidden Bearish
        if p_high2 < p_high1 and m_high2 > m_high1:
            return "Bearish RD"

    except Exception as e:
        logger.debug(f"Divergence calculation error: {e}")
        
    return None

def run_rsi_market_pulse(df):
    if len(df) < 14:
        return None, None
    rsi = talib.RSI(df["close"], 14).iloc[-1]
    if rsi > 60:
        zone = "RSI > 60"
    elif rsi < 40:
        zone = "RSI < 40"
    else:
        zone = "RSI 40–60"
    return round(rsi, 2), zone

def run_volume_shocker(df):
    if len(df) < 20:
        return False
    vol_sma = df["volume"].rolling(10).mean()
    last, prev = df.iloc[-1], df.iloc[-2]
    return (
        last["volume"] > 5 * vol_sma.iloc[-1]
        and prev["close"] * 0.95 <= last["close"] <= prev["close"] * 1.05
    )

def run_nrb_7(df):
    if len(df) < 20:
        return None
    base = df.iloc[-7]
    inside = df.iloc[-6:-1]
    last = df.iloc[-1]
    base_high, base_low = base["high"], base["low"]

    cond_high_low = inside["high"].max() <= base_high and inside["low"].min() >= base_low
    cond_open_close = (
        inside["open"].max() <= base_high
        and inside["open"].min() >= base_low
        and inside["close"].max() <= base_high
        and inside["close"].min() >= base_low
    )

    if not (cond_high_low and cond_open_close):
        return None

    avg_vol = df["volume"].rolling(10).mean().iloc[-2]
    if last["volume"] < 1.5 * avg_vol:
        return None

    if last["close"] > base_high:
        return "NRB-7 Bullish Breakout + Volume"
    if last["close"] < base_low:
        return "NRB-7 Bearish Breakdown + Volume"
    return None

def run_counter_attack(df):
    if len(df) < 2:
        return None
    prev, curr = df.iloc[-2], df.iloc[-1]
    mid = (prev["open"] + prev["close"]) / 2
    if prev["close"] < prev["open"] and curr["close"] > curr["open"]:
        if curr["open"] < prev["close"] and curr["close"] >= mid:
            return "Bullish"
    if prev["close"] > prev["open"] and curr["close"] < curr["open"]:
        if curr["open"] > prev["close"] and curr["close"] <= mid:
            return "Bearish"
    return None

def run_breakaway_gap(df):
    if len(df) < 50:
        return None
    df = df.copy()
    df["EMA20"] = talib.EMA(df["close"], 20)
    df["EMA50"] = talib.EMA(df["close"], 50)
    prev, curr = df.iloc[-2], df.iloc[-1]

    if curr["open"] > prev["high"] * 1.005 and curr["low"] > prev["high"]:
        if curr["EMA20"].iloc[-1] < curr["EMA50"].iloc[-1]:
            return "Bullish Breakaway Gap"
    if curr["open"] < prev["low"] * 0.995 and curr["high"] < prev["low"]:
        if curr["EMA20"].iloc[-1] > curr["EMA50"].iloc[-1]:
            return "Bearish Breakaway Gap"
    return None

def run_rsi_adx(df):
    if len(df) < 20:
        return None
    rsi = talib.RSI(df["close"], 14).iloc[-1]
    adx = talib.ADX(df["high"], df["low"], df["close"], 14).iloc[-1]
    if adx > 50 and rsi < 20:
        return "Bullish Reversal"
    if adx > 50 and rsi > 80:
        return "Probable Bearish Reversal"
    return None

def run_macd_market_pulse(df):
    if len(df) < 30:
        return None
    macd, signal, _ = talib.MACD(df["close"], 12, 26, 9)
    m, s = macd.iloc[-1], signal.iloc[-1]
    pm = macd.iloc[-2]

    if m > 0 and m > s and m > pm:
        return "Strong Bullish"
    if m > 0 and m > s and m < pm:
        return "Bullish Cooling"
    if m > 0 and m < s and m > pm:
        return "Bullish Reversal Watch"
    if m > 0 and m < s and m < pm:
        return "Weak Bullish"
    if m < 0 and m > s and m > pm:
        return "Bearish Reversal Watch"
    if m < 0 and m > s and m < pm:
        return "Weak Bearish"
    if m < 0 and m < s and m > pm:
        return "Bearish Recovery Attempt"
    if m < 0 and m < s and m < pm:
        return "Strong Bearish"
    return None

def run_trend_alignment(df):
    if len(df) < 100:
        return None
    ema20 = talib.EMA(df["close"], 20).iloc[-1]
    ema50 = talib.EMA(df["close"], 50).iloc[-1]
    ema100 = talib.EMA(df["close"], 100).iloc[-1]

    if ema20 > ema50 > ema100:
        return "Strong Uptrend"
    if ema20 < ema50 < ema100:
        return "Strong Downtrend"
    return None

def run_pullback_to_ema(df):
    if len(df) < 60:
        return None
    ema20 = talib.EMA(df["close"], 20).iloc[-1]
    ema50 = talib.EMA(df["close"], 50).iloc[-1]
    last = df.iloc[-1]

    if ema20 > ema50:
        if last["low"] <= ema20 and last["close"] > ema20:
            return "Bullish EMA Pullback"
    if ema20 < ema50:
        if last["high"] >= ema20 and last["close"] < ema20:
            return "Bearish EMA Pullback"
    return None

def run_confluence_setup(df):
    if len(df) < 60:
        return None
    rsi = talib.RSI(df["close"], 14).iloc[-1]
    macd, sig, _ = talib.MACD(df["close"], 12, 26, 9)
    ema20 = talib.EMA(df["close"], 20).iloc[-1]
    ema50 = talib.EMA(df["close"], 50).iloc[-1]

    if rsi > 50 and macd.iloc[-1] > sig.iloc[-1] and ema20 > ema50:
        return "Bullish Confluence"
    if rsi < 50 and macd.iloc[-1] < sig.iloc[-1] and ema20 < ema50:
        return "Bearish Confluence"
    return None

def run_macd_hook_up(df):
    if len(df) < 35:
        return None
    macd, signal, hist = talib.MACD(df["close"], 12, 26, 9)
    if (
        macd.iloc[-1] > 0
        and macd.iloc[-1] > signal.iloc[-1]
        and macd.iloc[-2] > signal.iloc[-2]
        and macd.iloc[-2] < macd.iloc[-3]
        and macd.iloc[-1] > macd.iloc[-2]
        and hist.iloc[-1] > hist.iloc[-2]
    ):
        return "MACD Hook Up"
    return None

def run_macd_hook_down(df):
    if len(df) < 35:
        return None
    macd, signal, hist = talib.MACD(df["close"], 12, 26, 9)
    if (
        macd.iloc[-1] < 0
        and macd.iloc[-1] < signal.iloc[-1]
        and macd.iloc[-2] < signal.iloc[-2]
        and macd.iloc[-2] > macd.iloc[-3]
        and macd.iloc[-1] < macd.iloc[-2]
        and hist.iloc[-1] < hist.iloc[-2]
    ):
        return "MACD Hook Down"
    return None

def run_ema50_stoch_oversold(df):
    if len(df) < 50:
        return None
    ema50 = talib.EMA(df["close"], 50).iloc[-1]
    slowk, slowd = talib.STOCH(
        df["high"], df["low"], df["close"], fastk_period=14, slowk_period=3, slowd_period=3
    )
    price = df["close"].iloc[-1]
    near_ema = abs(price - ema50) / ema50 <= 0.005
    stoch_cross = (
        slowk.iloc[-2] < slowd.iloc[-2]
        and slowk.iloc[-1] > slowd.iloc[-1]
        and slowk.iloc[-1] < 20
    )
    if near_ema and stoch_cross:
        return "EMA50 + Stoch Oversold Buy"
    return None

def run_kdj(df, period=9, signal=3):
    low_min = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    rng = (high_max - low_min).replace(0, np.nan)
    rsv = 100 * (df["close"] - low_min) / rng
    rsv = rsv.clip(lower=0, upper=100)

    def bcwsma(series, length):
        out = []
        for i, val in enumerate(series):
            if i == 0 or np.isnan(val):
                out.append(val)
            else:
                out.append((val + (length - 1) * out[-1]) / length)
        return pd.Series(out, index=series.index)

    pK = bcwsma(rsv, signal)
    pD = bcwsma(pK, signal)
    pJ = 3 * pK - 2 * pD
    return pK, pD, pJ

def run_kdj_buy(df):
    if len(df) < 20:
        return None
    pK, pD, pJ = run_kdj(df)
    if pd.isna(pD.iloc[-1]) or pd.isna(pJ.iloc[-1]):
        return None
    crossed_up = (pJ.iloc[-2] < pD.iloc[-2]) and (pJ.iloc[-1] > pD.iloc[-1])
    oversold = (pD.iloc[-1] < 30) and (pJ.iloc[-1] < 30)
    if crossed_up and oversold:
        return "KDJ BUY (J↑D oversold)"
    return None

def run_kdj_sell(df):
    if len(df) < 20:
        return None
    pK, pD, pJ = run_kdj(df)
    if pd.isna(pD.iloc[-1]) or pd.isna(pJ.iloc[-1]):
        return None
    crossed_down = (pJ.iloc[-2] > pD.iloc[-2]) and (pJ.iloc[-1] < pD.iloc[-1])
    overbought = (pD.iloc[-1] > 70) and (pJ.iloc[-1] > 70)
    if crossed_down and overbought:
        return "KDJ SELL (J↓D overbought)"
    return None

def run_macd_nd_filtered(df):
    div = detect_macd_divergence(df)
    if div:
        return {
            "Signal": "Divergence Alert",
            "Divergence": div
        }
    return None

SCANNERS = {
    "RSI Market Pulse": lambda df: {
        "Signal": "Monitor",
        "RSI": run_rsi_market_pulse(df)[0],
        "Zone": run_rsi_market_pulse(df)[1],
    },
    "Volume Shocker": lambda df: {
        "Signal": "BUY" if run_volume_shocker(df) else "Neutral",
        "Setup": "High Vol Expansion" if run_volume_shocker(df) else None
    },
    "NRB-7 breakout": lambda df: {
        "Signal": "BUY/SELL" if run_nrb_7(df) else "Neutral",
        "Setup": run_nrb_7(df)
    },
    "Counter Attack Pattern": lambda df: {
        "Signal": run_counter_attack(df) or "Neutral"
    },
    "Breakaway Gap": lambda df: {
        "Signal": "Alert",
        "Setup": run_breakaway_gap(df)
    },
    "RSI + ADX Extremes": lambda df: {
        "Signal": "Reversal Watch",
        "Setup": run_rsi_adx(df)
    },
    "MACD Market Pulse": lambda df: {
        "Signal": "Monitor",
        "Trend": run_macd_market_pulse(df)
    },
    "MACD Normal Divergence": lambda df: run_macd_nd_filtered(df),
    "Trend Alignment": lambda df: {
        "Signal": "Uptrend/Downtrend",
        "Trend": run_trend_alignment(df)
    },
    "Pullback to EMA": lambda df: {
        "Signal": "Pullback Check",
        "Setup": run_pullback_to_ema(df)
    },
    "Confluence": lambda df: {
        "Signal": "Confluence Detected",
        "Setup": run_confluence_setup(df)
    },
    "MACD Hook Up": lambda df: {
        "Signal": "BUY" if run_macd_hook_up(df) else "Neutral"
    },
    "MACD Hook Down": lambda df: {
        "Signal": "SELL" if run_macd_hook_down(df) else "Neutral"
    },
    "EMA50 + Stochastic": lambda df: {
        "Signal": "BUY" if run_ema50_stoch_oversold(df) else "Neutral"
    },
    "KDJ Cross Buy": lambda df: {
        "Signal": "BUY" if run_kdj_buy(df) else "Neutral"
    },
    "KDJ Cross Sell": lambda df: {
        "Signal": "SELL" if run_kdj_sell(df) else "Neutral"
    }
}

def extract_grid_cell_value(scanner_name, res):
    if not res:
        return ""
    if scanner_name == "Volume Shocker":
        return "High Vol Expansion" if res.get("Signal") == "BUY" else ""
    elif scanner_name == "NRB-7 breakout":
        return res.get("Setup") or ""
    elif scanner_name == "Counter Attack Pattern":
        sig = res.get("Signal")
        return sig if sig != "Neutral" else ""
    elif scanner_name in ["Breakaway Gap", "RSI + ADX Extremes", "Pullback to EMA", "Confluence"]:
        return res.get("Setup") or ""
    elif scanner_name in ["MACD Market Pulse", "Trend Alignment"]:
        return res.get("Trend") or ""
    elif scanner_name == "MACD Normal Divergence":
        return res.get("Divergence") or ""
    elif scanner_name in ["MACD Hook Up", "MACD Hook Down", "EMA50 + Stochastic", "KDJ Cross Buy", "KDJ Cross Sell"]:
        sig = res.get("Signal")
        return sig if sig != "Neutral" else ""
    elif scanner_name == "RSI Market Pulse":
        return res.get("Zone") or ""
    return str(res.get("Signal", ""))

# ==============================================================================
# 4. MULTI-TIMEFRAME ANALYTICS & DEDICATED MACD EXCEL GENERATOR
# ==============================================================================
def generate_analytics_data(tf_divergences):
    analytics_rows = []

    for htf, ltf in HTF_LTF_MAP:
        htf_data = tf_divergences.get(htf, {})
        ltf_data = tf_divergences.get(ltf, {})

        common_symbols = set(htf_data.keys()).intersection(set(ltf_data.keys()))

        for sym in common_symbols:
            htf_div = htf_data[sym]["Divergence"]
            ltf_div = ltf_data[sym]["Divergence"]

            bucket = None
            remark = ""
            recommendation = "NEUTRAL"

            if htf_div == "Bullish ND" and ltf_div == "Bullish ND":
                bucket = "Bullish ND + Bullish ND"
                remark = f"{sym}: HTF {htf} Bullish ND and LTF {ltf} Bullish ND (Strong Reversal Confluence)"
                recommendation = "STRONG BUY"

            elif htf_div == "Bullish RD" and ltf_div == "Bullish ND":
                bucket = "Bullish RD + Bullish ND"
                remark = f"{sym}: HTF {htf} Bullish RD and LTF {ltf} Bullish ND (Trend Continuation with Entry Signal)"
                recommendation = "BUY"

            elif htf_div == "Bearish ND" and ltf_div == "Bearish ND":
                bucket = "Bearish ND + Bearish ND"
                remark = f"{sym}: HTF {htf} Bearish ND and LTF {ltf} Bearish ND (Strong Distribution Signal)"
                recommendation = "STRONG SELL"

            elif htf_div == "Bearish RD" and ltf_div == "Bearish ND":
                bucket = "Bearish RD + Bearish ND"
                remark = f"{sym}: HTF {htf} Bearish RD and LTF {ltf} Bearish ND (Downtrend Continuation Signal)"
                recommendation = "SELL"

            if bucket:
                analytics_rows.append({
                    "Symbol": sym,
                    "HTF": htf,
                    "HTF Divergence": htf_div,
                    "LTF": ltf,
                    "LTF Divergence": ltf_div,
                    "Combination Category": bucket,
                    "Recommendation": recommendation,
                    "Remarks": remark,
                    "TV_Link": make_tradingview_link(sym)
                })

    return pd.DataFrame(analytics_rows)

def generate_dedicated_macd_excel(tf_divergences, date_str):
    """
    Creates a dedicated standalone Excel file specifically for MACD Divergences across all timeframes.
    """
    macd_filename = f"MACD_Divergence_Report_{date_str}.xlsx"
    macd_filepath = os.path.join(OUTPUT_DIR, macd_filename)
    
    with pd.ExcelWriter(macd_filepath, engine="openpyxl") as writer:
        for tf_label, div_dict in tf_divergences.items():
            rows = []
            for sym, data in div_dict.items():
                rows.append({
                    "Symbol": sym,
                    "Timeframe": tf_label,
                    "Divergence Type": data.get("Divergence", ""),
                    "Signal": data.get("Signal", "Divergence Alert"),
                    "TV_Link": make_tradingview_link(sym)
                })
            
            df_tf = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["Symbol", "Timeframe", "Divergence Type", "Signal", "TV_Link"])
            if df_tf.empty:
                df_tf = pd.DataFrame([["No MACD Divergence detected for this timeframe", tf_label, "", "", ""]], 
                                     columns=["Symbol", "Timeframe", "Divergence Type", "Signal", "TV_Link"])
            
            df_tf.to_excel(writer, sheet_name=tf_label, index=False)
            
    logger.info(f"Generated Dedicated MACD Divergence File: {macd_filepath}")
    return macd_filepath

# ==============================================================================
# 5. BATCH PROCESSING ENGINE
# ==============================================================================
def process_timeframe(folder_name, output_name, date_str):
    folder_path = os.path.join(BASE_PATH, folder_name)
    if not os.path.exists(folder_path):
        logger.warning(f"Skipping {folder_name}: Directory not found.")
        return None, {}, pd.DataFrame()

    files = [f for f in os.listdir(folder_path) if f.endswith(".parquet")]
    if not files:
        logger.warning(f"Skipping {folder_name}: No parquet files found.")
        return None, {}, pd.DataFrame()

    logger.info(f"Scanning {len(files)} symbols in {folder_name}...")
    sheets_data = {scanner_name: [] for scanner_name in SCANNERS.keys()}
    grid_rows = []
    divergence_dict = {}

    for f in files:
        sym = f.replace(".parquet", "")
        try:
            df = pd.read_parquet(os.path.join(folder_path, f))
            if df.empty or len(df) < 50:
                continue

            grid_stock_row = {"Symbol": sym}

            for scanner_name, scanner_fn in SCANNERS.items():
                try:
                    res = scanner_fn(df)
                    grid_stock_row[scanner_name] = extract_grid_cell_value(scanner_name, res)
                    
                    if res:
                        row = {col: "" for col in SAFE_COLS}
                        row["Symbol"] = sym
                        row["TV_Link"] = make_tradingview_link(sym)
                        
                        for k, v in res.items():
                            if k in row and v is not None:
                                row[k] = v
                                
                        if any(row[col] not in ["", "Neutral", None] for col in ["Signal", "Setup", "Divergence", "Trend"]):
                            sheets_data[scanner_name].append(row)
                            
                        if scanner_name == "MACD Normal Divergence" and res.get("Divergence"):
                            divergence_dict[sym] = row
                except Exception as e:
                    logger.debug(f"Failed to scan {sym} with {scanner_name}: {e}")
                    
            grid_rows.append(grid_stock_row)
                        
        except Exception as e:
            logger.error(f"Error loading file {f}: {e}")

    excel_filename = f"{output_name}_{date_str}.xlsx"
    excel_filepath = os.path.join(OUTPUT_DIR, excel_filename)
    
    with pd.ExcelWriter(excel_filepath, engine="openpyxl") as writer:
        for scanner_name, rows in sheets_data.items():
            df_out = pd.DataFrame(rows) if rows else pd.DataFrame(columns=SAFE_COLS)
            if df_out.empty:
                df_out = pd.DataFrame([["No scanner alerts detected for this interval", ""] + [""] * 10], columns=SAFE_COLS)
            df_out.to_excel(writer, sheet_name=scanner_name[:30], index=False)

    logger.info(f"Successfully generated: {excel_filepath}")
    df_grid_tf = pd.DataFrame(grid_rows) if grid_rows else pd.DataFrame(columns=["Symbol"] + list(SCANNERS.keys()))
    return excel_filepath, divergence_dict, df_grid_tf

# ==============================================================================
# 6. EXCLUSIVE HTML DASHBOARD BLOCK BUILDER
# ==============================================================================
def build_html_dashboard(grid_dfs, analytics_df, date_str):
    total_stocks_tracked = 0
    top_picks_list = []
    tf_summary_html = ""
    
    for tf, df in grid_dfs.items():
        if df.empty:
            continue
            
        total_stocks_tracked = max(total_stocks_tracked, len(df))
        
        rsi_bullish = len(df[df["RSI Market Pulse"] == "RSI > 60"]) if "RSI Market Pulse" in df else 0
        rsi_bearish = len(df[df["RSI Market Pulse"] == "RSI < 40"]) if "RSI Market Pulse" in df else 0
        rsi_neutral = len(df[df["RSI Market Pulse"] == "RSI 40–60"]) if "RSI Market Pulse" in df else 0
        total_rsi = max(1, rsi_bullish + rsi_bearish + rsi_neutral)
        
        bullish_pct = int((rsi_bullish / total_rsi) * 100)
        neutral_pct = int((rsi_neutral / total_rsi) * 100)
        bearish_pct = int((rsi_bearish / total_rsi) * 100)
        
        macd_strong_bull = len(df[df["MACD Market Pulse"] == "Strong Bullish"]) if "MACD Market Pulse" in df else 0
        macd_weak_bull = len(df[df["MACD Market Pulse"] == "Weak Bullish"]) if "MACD Market Pulse" in df else 0
        macd_strong_bear = len(df[df["MACD Market Pulse"] == "Strong Bearish"]) if "MACD Market Pulse" in df else 0
        macd_cooling = len(df[df["MACD Market Pulse"] == "Bullish Cooling"]) if "MACD Market Pulse" in df else 0
        
        for _, row in df.iterrows():
            conditions = [
                "Bullish Confluence" in str(row.get("Confluence", "")),
                "High Vol Expansion" in str(row.get("Volume Shocker", "")),
                "Bullish" in str(row.get("MACD Normal Divergence", "")),
                "MACD Hook Up" in str(row.get("MACD Hook Up", ""))
            ]
            if any(conditions) and len(top_picks_list) < 8:
                reasons = []
                if conditions[0]: reasons.append("Confluence")
                if conditions[1]: reasons.append("Vol Expansion")
                if conditions[2]: reasons.append("MACD Divergence")
                if conditions[3]: reasons.append("MACD Hook Up")
                
                pick_info = {
                    "symbol": row["Symbol"],
                    "tf": tf,
                    "reason": " + ".join(reasons),
                    "link": make_tradingview_link(row["Symbol"])
                }
                if pick_info not in top_picks_list:
                    top_picks_list.append(pick_info)

        tf_summary_html += f"""
        <div style="background-color: #ffffff; padding: 18px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #eef2f5;">
            <h3 style="margin-top: 0; color: #1e293b; border-bottom: 2px solid #3b82f6; padding-bottom: 6px; display: inline-block;">🕒 {tf} Timeframe Pulse</h3>
            
            <p style="margin: 10px 0 5px 0; font-size: 13px; color: #64748b; font-weight: 600;">RSI MARKET DYNAMICS</p>
            <div style="width: 100%; background-color: #e2e8f0; border-radius: 4px; height: 16px; display: flex; overflow: hidden; margin-bottom: 12px;">
                <div style="width: {bullish_pct}%; background-color: #10b981; color: white; font-size: 10px; text-align: center; line-height: 16px; font-weight: bold;">{bullish_pct}%</div>
                <div style="width: {neutral_pct}%; background-color: #94a3b8; color: white; font-size: 10px; text-align: center; line-height: 16px; font-weight: bold;">{neutral_pct}%</div>
                <div style="width: {bearish_pct}%; background-color: #ef4444; color: white; font-size: 10px; text-align: center; line-height: 16px; font-weight: bold;">{bearish_pct}%</div>
            </div>
            <table style="width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 15px;">
                <tr>
                    <td>🟢 <span style="color:#10b981; font-weight:bold;">Bullish (>60):</span> {rsi_bullish} stocks</td>
                    <td>⚪ <span style="color:#64748b; font-weight:bold;">Neutral (40-60):</span> {rsi_neutral} stocks</td>
                    <td>🔴 <span style="color:#ef4444; font-weight:bold;">Bearish (<40):</span> {rsi_bearish} stocks</td>
                </tr>
            </table>

            <p style="margin: 0 0 8px 0; font-size: 13px; color: #64748b; font-weight: 600;">MACD RANGE PULSE</p>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; background-color: #f8fafc; border-radius: 6px;">
                <thead>
                    <tr style="background-color: #cbd5e1; color: #334155;">
                        <th style="padding: 6px 10px;">Strong Bullish</th>
                        <th style="padding: 6px 10px;">Bullish Cooling</th>
                        <th style="padding: 6px 10px;">Weak Bullish</th>
                        <th style="padding: 6px 10px;">Strong Bearish</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td style="padding: 8px 10px; color: #047857; font-weight: bold;">{macd_strong_bull}</td>
                        <td style="padding: 8px 10px; color: #b45309;">{macd_cooling}</td>
                        <td style="padding: 8px 10px; color: #059669;">{macd_weak_bull}</td>
                        <td style="padding: 8px 10px; color: #b91c1c; font-weight: bold;">{macd_strong_bear}</td>
                    </tr>
                </tbody>
            </table>
        </div>
        """

    htpl_rows = ""
    if not analytics_df.empty:
        for _, row in analytics_df.iterrows():
            rec_color = "#10b981" if "BUY" in row["Recommendation"] else "#ef4444"
            htpl_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px; font-weight: bold; color: #1e3a8a;">{row['Symbol']}</td>
                <td style="padding: 10px; font-size: 12px;">{row['HTF']} ({row['HTF Divergence']}) / {row['LTF']} ({row['LTF Divergence']})</td>
                <td style="padding: 10px;"><span style="color: {rec_color}; font-weight: bold; font-size: 12px;">{row['Recommendation']}</span></td>
                <td style="padding: 10px; color: #475569; font-size: 12px;">{row['Remarks']}</td>
                <td style="padding: 10px;"><a href="{row['TV_Link']}" style="color: #3b82f6; text-decoration: none; font-weight: bold;" target="_blank">Chart ↗</a></td>
            </tr>
            """
    else:
        htpl_rows = """<tr><td colspan="5" style="padding: 15px; text-align: center; color: #94a3b8;">No HTF-LTF confluence patterns matched across current cycles.</td></tr>"""

    top_picks_rows = ""
    if top_picks_list:
        for pick in top_picks_list:
            top_picks_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px; font-weight: bold; color: #1e3a8a;">{pick['symbol']}</td>
                <td style="padding: 10px;"><span style="background-color: #dbeafe; color: #1e40af; padding: 2px 6px; border-radius: 4px; font-size: 11px;">{pick['tf']}</span></td>
                <td style="padding: 10px; color: #475569; font-size: 12px;">✨ {pick['reason']}</td>
                <td style="padding: 10px;"><a href="{pick['link']}" style="color: #3b82f6; text-decoration: none; font-weight: bold;" target="_blank">Chart ↗</a></td>
            </tr>
            """
    else:
        top_picks_rows = """<tr><td colspan="4" style="padding: 15px; text-align: center; color: #94a3b8;">No immediate breakthrough setups detected today. Check full sheets.</td></tr>"""

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>NSE Executive Market Dashboard</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; margin: 0; padding: 20px; color: #334155;">
        <div style="max-width: 750px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
            
            <div style="background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); padding: 25px; color: #ffffff; text-align: center;">
                <h1 style="margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.5px;">📊 NSE Automated Scanner Dashboard</h1>
                <p style="margin: 6px 0 0 0; opacity: 0.9; font-size: 14px;">Market Analytics & HTPL Divergence Dashboard &bull; {date_str}</p>
            </div>
            
            <div style="padding: 20px; background-color: #f8fafc; display: flex; justify-content: space-between; border-bottom: 1px solid #e2e8f0; gap: 10px;">
                <div style="background-color: white; padding: 10px 15px; border-radius: 6px; border-left: 4px solid #3b82f6; width: 45%;">
                    <div style="font-size: 11px; color: #64748b; font-weight: bold; text-transform: uppercase;">Stocks Monitored</div>
                    <div style="font-size: 20px; font-weight: bold; color: #1e293b;">{total_stocks_tracked} Symbols</div>
                </div>
                <div style="background-color: white; padding: 10px 15px; border-radius: 6px; border-left: 4px solid #10b981; width: 45%;">
                    <div style="font-size: 11px; color: #64748b; font-weight: bold; text-transform: uppercase;">Pipeline Run Status</div>
                    <div style="font-size: 20px; font-weight: bold; color: #10b981;">SUCCESS ✅</div>
                </div>
            </div>

            <div style="padding: 20px;">
                <h2 style="font-size: 16px; color: #1e3a8a; text-transform: uppercase; margin-top: 0; margin-bottom: 12px; border-left: 4px solid #3b82f6; padding-left: 8px;">🎯 HTPL Dashboard (Buy / Sell Recommendations)</h2>
                <table style="width: 100%; border-collapse: collapse; text-align: left; margin-bottom: 25px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; overflow: hidden;">
                    <thead>
                        <tr style="background-color: #f1f5f9; color: #475569; font-size: 12px;">
                            <th style="padding: 8px 10px;">Stock</th>
                            <th style="padding: 8px 10px;">Pairing & Signals</th>
                            <th style="padding: 8px 10px;">Recommendation</th>
                            <th style="padding: 8px 10px;">Remarks</th>
                            <th style="padding: 8px 10px;">Chart</th>
                        </tr>
                    </thead>
                    <tbody>
                        {htpl_rows}
                    </tbody>
                </table>

                <h2 style="font-size: 16px; color: #1e3a8a; text-transform: uppercase; margin-bottom: 12px; border-left: 4px solid #1e3a8a; padding-left: 8px;">⭐ Top Picks / Technical Confluences</h2>
                <table style="width: 100%; border-collapse: collapse; text-align: left; margin-bottom: 25px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; overflow: hidden;">
                    <thead>
                        <tr style="background-color: #f1f5f9; color: #475569; font-size: 13px;">
                            <th style="padding: 10px;">Stock</th>
                            <th style="padding: 10px;">Interval</th>
                            <th style="padding: 10px;">Matching Technical Triggers</th>
                            <th style="padding: 10px;">TradingView</th>
                        </tr>
                    </thead>
                    <tbody style="font-size: 13px;">
                        {top_picks_rows}
                    </tbody>
                </table>

                <h2 style="font-size: 16px; color: #1e3a8a; text-transform: uppercase; margin-bottom: 12px; border-left: 4px solid #1e3a8a; padding-left: 8px;">📈 Market Pulse Heatmaps</h2>
                {tf_summary_html}
                
                <div style="margin-top: 25px; padding-top: 15px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #94a3b8; text-align: center;">
                    <p>This automated email dashboard tracks key structural indicators across multiple custom target windows.<br/>
                    Please refer to the attached separate Excel documents for comprehensive scanning row matrices.</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html_body

# ==============================================================================
# 7. TELEGRAM & EMAIL NOTIFICATION ENGINE
# ==============================================================================
def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req) as resp:
                pass
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")

def send_email_with_attachments(subject, html_content, attachments):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(RECIPIENTS)
    if BCC_RECIPIENTS:
        msg["Bcc"] = ", ".join(BCC_RECIPIENTS)

    msg.set_content("Please enable HTML view in your email client to view the executive report.")
    msg.add_alternative(html_content, subtype="html")

    for filepath in attachments:
        if filepath and os.path.exists(filepath):
            filename = os.path.basename(filepath)
            ctype, encoding = mimetypes.guess_type(filepath)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)

            with open(filepath, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename
                )

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        logger.info("Executive email with attachments delivered successfully.")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

# ==============================================================================
# 8. MAIN EXECUTION PIPELINE
# ==============================================================================
def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Starting Scan Pipeline for {date_str}...")

    tf_divergences = {}
    grid_dfs = {}
    generated_excel_files = []

    # Step A: Run processing per timeframe
    for tf_label, (folder, out_prefix) in TIMEFRAME_FOLDERS.items():
        filepath, divergences, df_grid = process_timeframe(folder, out_prefix, date_str)
        if filepath:
            generated_excel_files.append(filepath)
        tf_divergences[tf_label] = divergences
        grid_dfs[tf_label] = df_grid

    # Step B: Generate Dedicated MACD Divergence Excel File
    macd_excel_path = generate_dedicated_macd_excel(tf_divergences, date_str)
    if macd_excel_path:
        generated_excel_files.append(macd_excel_path)

    # Step C: Generate HTF vs LTF Analytics Dataframe
    analytics_df = generate_analytics_data(tf_divergences)

    # Step D: Append HTF_LTF Analytics Sheet into generated timeframe Excel files
    for filepath in generated_excel_files:
        if "MACD_Divergence_Report" in filepath:
            continue
        try:
            with pd.ExcelWriter(filepath, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                if not analytics_df.empty:
                    analytics_df.to_excel(writer, sheet_name="HTF_LTF_Analytics", index=False)
                else:
                    empty_analytics = pd.DataFrame([["No multi-timeframe divergence confluences detected", "", "", "", "", "", "", "", ""]], 
                                                   columns=["Symbol", "HTF", "HTF Divergence", "LTF", "LTF Divergence", "Combination Category", "Recommendation", "Remarks", "TV_Link"])
                    empty_analytics.to_excel(writer, sheet_name="HTF_LTF_Analytics", index=False)
            logger.info(f"Added HTF_LTF_Analytics sheet to {os.path.basename(filepath)}")
        except Exception as e:
            logger.error(f"Could not append HTF_LTF_Analytics sheet to {filepath}: {e}")

    # Step E: Build HTML Dashboard
    html_dashboard = build_html_dashboard(grid_dfs, analytics_df, date_str)

    # Step F: Dispatch Alerts & Notifications
    email_subject = f"NSE Scanner Executive Report & HTPL Dashboard - {date_str}"
    send_email_with_attachments(email_subject, html_dashboard, generated_excel_files)

    tg_summary = f"<b>NSE Market Scan Complete ({date_str})</b>\n\n"
    if not analytics_df.empty:
        tg_summary += f"<b>HTPL Alerts:</b> {len(analytics_df)} confluence signals found!\n"
        for _, r in analytics_df.head(5).iterrows():
            tg_summary += f"• <b>{r['Symbol']}</b>: {r['Recommendation']} ({r['Combination Category']})\n"
    else:
        tg_summary += "No multi-timeframe divergence confluences detected today.\n"
    
    send_telegram_message(tg_summary)
    logger.info("Pipeline execution completed successfully.")

if __name__ == "__main__":
    main()
