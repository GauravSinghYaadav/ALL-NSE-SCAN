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
# 2. GLOBAL CONFIGURATION
# ==============================================================================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "nse.scanner.app@gmail.com"
SENDER_PASSWORD = "wmkdozoyfprduqgx"
RECIPIENTS = ["yadav.gauravsingh@gmail.com", "dipti.gorwadia@gmail.com"]

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
        last["volume"] > 2 * vol_sma.iloc[-1]
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

def run_macd_normal_divergence(df, lookback=30):
    if len(df) < lookback:
        return None
    macd, _, _ = talib.MACD(df["close"], 12, 26, 9)
    price_low1 = df["low"].iloc[-lookback:-15].min()
    price_low2 = df["low"].iloc[-15:].min()
    macd_low1 = macd.iloc[-lookback:-15].min()
    macd_low2 = macd.iloc[-15:].min()

    if price_low2 < price_low1 and macd_low2 > macd_low1:
        return "Bullish ND"

    price_high1 = df["high"].iloc[-lookback:-15].max()
    price_high2 = df["high"].iloc[-15:].max()
    macd_high1 = macd.iloc[-lookback:-15].max()
    macd_high2 = macd.iloc[-15:].max()

    if price_high2 > price_high1 and macd_high2 < macd_high1:
        return "Bearish ND"
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
    nd = run_macd_normal_divergence(df)
    if nd:
        return {
            "Signal": "Divergence Alert",
            "Divergence": nd
        }
    return None

# Dictionary linking Worksheet Name to Scanner Execution Method
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

# Helper method to map single-value primary scanner outputs for the Summary Grid Layout Matrix
def extract_grid_cell_value(scanner_name, res):
    if not res:
        return ""
    
    # Custom display logic per column based on your attachment sample layouts
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
# 4. BATCH PROCESSING ENGINE
# ==============================================================================
def process_timeframe(folder_name, output_name, date_str):
    folder_path = os.path.join(BASE_PATH, folder_name)
    if not os.path.exists(folder_path):
        logger.warning(f"Skipping {folder_name}: Directory not found.")
        return None, [], pd.DataFrame()

    files = [f for f in os.listdir(folder_path) if f.endswith(".parquet")]
    if not files:
        logger.warning(f"Skipping {folder_name}: No parquet files found.")
        return None, [], pd.DataFrame()

    logger.info(f"Scanning {len(files)} symbols in {folder_name}...")
    
    sheets_data = {scanner_name: [] for scanner_name in SCANNERS.keys()}
    
    # Container for building the requested single comprehensive spreadsheet mapping matrix grid
    grid_rows = []

    for f in files:
        sym = f.replace(".parquet", "")
        try:
            df = pd.read_parquet(os.path.join(folder_path, f))
            if df.empty or len(df) < 50:
                continue

            # Base grid structure for this stock
            grid_stock_row = {"Symbol": sym}

            for scanner_name, scanner_fn in SCANNERS.items():
                try:
                    res = scanner_fn(df)
                    
                    # Store cell mapping value for the single summary matrix sheet view
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
                except Exception as e:
                    logger.debug(f"Failed to scan {sym} with {scanner_name}: {e}")
                    
            grid_rows.append(grid_stock_row)
                        
        except Exception as e:
            logger.error(f"Error loading file {f}: {e}")

    # Create detailed separate workbook for individual scanners inside this timeframe
    excel_filename = f"{output_name}_{date_str}.xlsx"
    excel_filepath = os.path.join(OUTPUT_DIR, excel_filename)
    
    with pd.ExcelWriter(excel_filepath, engine="openpyxl") as writer:
        for scanner_name, rows in sheets_data.items():
            df_out = pd.DataFrame(rows) if rows else pd.DataFrame(columns=SAFE_COLS)
            if df_out.empty:
                df_out = pd.DataFrame([["No scanner alerts detected for this interval", ""] + [""] * 10], columns=SAFE_COLS)
            df_out.to_excel(writer, sheet_name=scanner_name[:30], index=False)

    logger.info(f"Successfully generated: {excel_filepath}")
    
    # Formulate the compiled dataframe representation for the timeline grid
    df_grid_tf = pd.DataFrame(grid_rows) if grid_rows else pd.DataFrame(columns=["Symbol"] + list(SCANNERS.keys()))
    
    return excel_filepath, sheets_data.get("MACD Normal Divergence", []), df_grid_tf

# ==============================================================================
# 5. COMMUNICATIONS (EMAIL & TELEGRAM MODULES)
# ==============================================================================
def send_email_with_attachments(file_paths, date_str):
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        logger.error("Email credentials missing. Skipping email dispatch.")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"NSE Scanner Report - {date_str}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(RECIPIENTS)
    msg.set_content(
        f"Hi,\n\nPlease find attached the NSE automated multi-timeframe stock reports for {date_str}.\n\n"
        f"Generated reports include:\n" + "\n".join([f"- {os.path.basename(p)}" for p in file_paths]) +
        f"\n\nRegards,\nAutomation Script"
    )

    for path in file_paths:
        if not os.path.exists(path):
            continue
            
        ctype, encoding = mimetypes.guess_type(path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=os.path.basename(path)
            )

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        logger.info("Notification email dispatched successfully.")
        return True
    except Exception as e:
        logger.error(f"SMTP Email Delivery failed: {e}")
        return False

def send_telegram_notification(date_str, report_count, total_scanners):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        logger.warning("Telegram details are unconfigured. Skipping Telegram alert.")
        return

    text = (
        f"✅ *NSE Scanner Completed*\n\n"
        f"📅 *Date :* {date_str}\n\n"
        f"*Generated Reports:*\n"
        f"✔ 15 Min\n"
        f"✔ Hourly\n"
        f"✔ Daily\n"
        f"✔ Weekly\n"
        f"✔ Monthly\n"
        f"✔ Comprehensive Summary Matrix Grid\n\n"
        f"📈 *Total Scanners :* {total_scanners}\n\n"
        f"✉ *Status :* {report_count} Reports mailed successfully."
    )

    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }).encode("utf-8")
            
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req) as response:
                response.read()
            logger.info(f"Telegram notification sent to Chat ID: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")

# ==============================================================================
# 6. MAIN CONTROLLER PIPELINE
# ==============================================================================
def main():
    start_time = datetime.now()
    date_str = start_time.strftime("%d %b %Y")
    logger.info(f"=== Starting NSE Batch Scanner Pipeline ({date_str}) ===")

    generated_files = []
    macd_divergences_by_tf = {}  
    grid_dataframes_by_tf = {} # Dictionary containing time-frame grid sheets

    for tf_key, (folder_name, output_name) in TIMEFRAME_FOLDERS.items():
        filepath, macd_rows, df_grid_tf = process_timeframe(folder_name, output_name, date_str)
        if filepath:
            generated_files.append(filepath)
            macd_divergences_by_tf[tf_key] = macd_rows
            if not df_grid_tf.empty:
                grid_dataframes_by_tf[tf_key] = df_grid_tf

    # NEW FILE GENERATION: Consolidated comprehensive matrix mapping grid sheet timeframe-wise
    if grid_dataframes_by_tf:
        summary_matrix_filename = f"Scanner Summary Matrix_{date_str}.xlsx"
        summary_matrix_filepath = os.path.join(OUTPUT_DIR, summary_matrix_filename)
        
        with pd.ExcelWriter(summary_matrix_filepath, engine="openpyxl") as writer:
            for tf_key, df_matrix in grid_dataframes_by_tf.items():
                # Order columns logically: Symbol followed by all scanner outputs
                cols_order = ["Symbol"] + [c for c in df_matrix.columns if c != "Symbol"]
                df_matrix = df_matrix[cols_order]
                
                # Each timeframe sheet contains all 214 stocks mapped across all scanner signals
                df_matrix.to_excel(writer, sheet_name=tf_key, index=False)
                
        logger.info(f"Successfully generated full mapping Summary Matrix: {summary_matrix_filepath}")
        generated_files.append(summary_matrix_filepath)

    # Generate the combined "MACD Normal Divergences" file with sheets for each timeframe
    if macd_divergences_by_tf:
        combined_macd_filename = f"MACD Normal Divergences_{date_str}.xlsx"
        combined_macd_filepath = os.path.join(OUTPUT_DIR, combined_macd_filename)
        
        with pd.ExcelWriter(combined_macd_filepath, engine="openpyxl") as writer:
            for tf_key, rows in macd_divergences_by_tf.items():
                df_out = pd.DataFrame(rows) if rows else pd.DataFrame(columns=SAFE_COLS)
                if df_out.empty:
                    df_out = pd.DataFrame([["No scanner alerts detected for this interval", ""] + [""] * 10], columns=SAFE_COLS)
                df_out.to_excel(writer, sheet_name=tf_key, index=False)
                
        logger.info(f"Successfully generated combined MACD report: {combined_macd_filepath}")
        generated_files.append(combined_macd_filepath)

    if not generated_files:
        logger.error("No reports were generated. Aborting email and Telegram alerts.")
        return

    # Dispatch Mail with All Attachments (including the combined MACD file & Summary Matrix)
    mail_success = send_email_with_attachments(generated_files, date_str)
    
    # Send Telegram Alerts
    if mail_success:
        send_telegram_notification(
            date_str=date_str, 
            report_count=len(generated_files), 
            total_scanners=len(SCANNERS)
        )

    end_time = datetime.now()
    duration = end_time - start_time
    logger.info(f"=== Pipeline completed successfully in {duration.total_seconds():.2f} seconds ===")

if __name__ == "__main__":
    main()
