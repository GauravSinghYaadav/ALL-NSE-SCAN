import os
import time
import socket
import ssl
from datetime import datetime
from tvDatafeed import TvDatafeed, Interval

# ==============================
# TradingView Credentials
# ==============================
USERNAME = os.getenv("TV_USERNAME", "EGAVSIV")
PASSWORD = os.getenv("TV_PASSWORD", "Eric$1234")

# ==============================
# Timeframes
# ==============================
TIMEFRAMES = {
    "D": (Interval.in_daily, "stock_data_D"),
    "W": (Interval.in_weekly, "stock_data_W"),
    "M": (Interval.in_monthly, "stock_data_M"),
}

BARS = 4000
RETRY_DELAY = 3
MAX_RETRY = 5

# ==============================
# Symbols
# ==============================
symbols = [
    "20MICRONS",
    "21STCENMGM",
    "360ONE",
    "3BBLACKBIO",
    "3IINFOLTD",
    "3MINDIA",
    "3PLAND",
    "5PAISA"
]

LOG_FILE = "download_log.txt"
ERROR_FILE = "error_symbols.txt"


def log(msg):
    print(msg)
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.now()} | {msg}\n")


def log_error(symbol, tf, err):
    with open(ERROR_FILE, "a") as f:
        f.write(f"{symbol},{tf},{err}\n")


def fetch_save(symbol, tf_label, interval, folder):

    os.makedirs(folder, exist_ok=True)

    tv = TvDatafeed(USERNAME, PASSWORD)

    for attempt in range(1, MAX_RETRY + 1):

        try:

            df = tv.get_hist(
                symbol=symbol,
                exchange="NSE",
                interval=interval,
                n_bars=BARS,
            )

            if df is not None and not df.empty:

                filename = os.path.join(folder, f"{symbol}.parquet")

                df.to_parquet(filename)

                print(f"Saved -> {filename}")

                log(f"[OK] {symbol} {tf_label}")

                return

            else:

                log(f"[EMPTY] {symbol} {tf_label} Attempt {attempt}")

        except (socket.timeout, ssl.SSLError):

            log(f"[TIMEOUT] {symbol} {tf_label}")

        except Exception as e:

            log(f"[ERROR] {symbol} {tf_label} {e}")

        time.sleep(RETRY_DELAY)

    log_error(symbol, tf_label, "Failed")


def run_all():

    log("===== DOWNLOAD STARTED =====")

    for tf_label, (interval, folder) in TIMEFRAMES.items():

        for symbol in symbols:

            fetch_save(symbol, tf_label, interval, folder)

    log("===== DOWNLOAD FINISHED =====")


if __name__ == "__main__":
    run_all()
