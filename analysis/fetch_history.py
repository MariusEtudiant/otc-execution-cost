"""Download six months of 1-minute candles for BTC and ETH from Binance and Coinbase.

Binance: monthly and daily zip archives from data.binance.vision (spot, USDT pairs).
Coinbase: public candles endpoint, 300 candles per call (USD pairs).
Output: data/history/{venue}_{asset}_1m.parquet with ts (UTC), open, high, low, close, volume (base),
quote_volume (USD or USDT) and, for Binance, number of trades and taker buy volume.
"""

import datetime as dt
import io
import os
import sys
import time
import zipfile

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "history")
START = dt.date(2026, 4, 1)
END = dt.date(2026, 10, 12)  # last full day
HEADERS = {"User-Agent": "otc-execution-cost/0.1 (research)"}
BINANCE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
                "n_trades", "taker_buy_base", "taker_buy_quote", "ignore"]

session = requests.Session()
session.headers.update(HEADERS)


def binance(symbol):
    frames = []
    month = START.replace(day=1)
    while month < END.replace(day=1):
        url = (f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/"
               f"{symbol}-1m-{month:%Y-%m}.zip")
        frames.append(read_zip(url))
        month = (month + dt.timedelta(days=32)).replace(day=1)
    day = END.replace(day=1)
    while day <= END:
        url = (f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/"
               f"{symbol}-1m-{day:%Y-%m-%d}.zip")
        frames.append(read_zip(url))
        day += dt.timedelta(days=1)
    df = pd.concat(frames, ignore_index=True)
    # Binance switched spot archives to microseconds in 2025; handle both units.
    unit = "us" if df["open_time"].iloc[0] > 1e14 else "ms"
    df["ts"] = pd.to_datetime(df["open_time"], unit=unit, utc=True)
    df = df[["ts", "open", "high", "low", "close", "volume", "quote_volume", "n_trades", "taker_buy_quote"]]
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def read_zip(url):
    r = session.get(url, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, names=BINANCE_COLS)
    if not str(df.iloc[0, 0]).isdigit():  # some archives carry a header row
        df = df.iloc[1:].astype(float)
    print("  ", url.rsplit("/", 1)[1], len(df), flush=True)
    return df.astype({"open_time": "int64"})


def coinbase(product):
    rows = []
    start = dt.datetime.combine(START, dt.time(), dt.timezone.utc)
    stop = dt.datetime.combine(END + dt.timedelta(days=1), dt.time(), dt.timezone.utc)
    step = dt.timedelta(minutes=300)
    t = start
    while t < stop:
        end = min(t + step - dt.timedelta(minutes=1), stop - dt.timedelta(minutes=1))
        for attempt in range(5):
            r = session.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                            params={"granularity": 60, "start": t.isoformat(), "end": end.isoformat()}, timeout=30)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            rows += r.json()
            break
        t += step
        time.sleep(0.12)
        if t.minute == 0 and t.hour == 0 and t.day == 1:
            print("  ", product, t.date(), len(rows), flush=True)
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["quote_volume"] = df["volume"] * (df["high"] + df["low"] + df["close"]) / 3  # typical-price approximation
    df = df[["ts", "open", "high", "low", "close", "volume", "quote_volume"]]
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = sys.argv[1:] or ["binance", "coinbase"]
    if "binance" in jobs:
        for asset in ("BTC", "ETH"):
            print("binance", asset, flush=True)
            binance(f"{asset}USDT").to_parquet(os.path.join(OUT, f"binance_{asset}_1m.parquet"))
    if "coinbase" in jobs:
        for asset in ("BTC", "ETH"):
            print("coinbase", asset, flush=True)
            coinbase(f"{asset}-USD").to_parquet(os.path.join(OUT, f"coinbase_{asset}_1m.parquet"))
    print("done", flush=True)


if __name__ == "__main__":
    main()
