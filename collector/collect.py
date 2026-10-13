"""Minute-by-minute order book collector for BTC and ETH on Binance, Coinbase and Kraken.

Every minute (aligned on :00 UTC) the six books are fetched in parallel. For each one we
store a truncated raw copy (levels within RAW_BAND_BPS of mid, gzip, one file per hour)
and append execution metrics to data/metrics.csv:

- mid, spread, depth within 5/10/25/50 bps of mid
- cost of sweeping a buy or sell of 0.1 to 10 M$ (VWAP vs mid, in bps)

Costs are measured against each venue's own mid, so the USDT/USD basis between Binance
and the USD venues does not leak into the comparison. A consolidated "ALL3" book merges
the three venues in bps space to measure the cost of splitting an order across them.

Stop cleanly by creating data/STOP.
"""

import concurrent.futures as cf
import csv
import ctypes
import datetime as dt
import gzip
import json
import logging
import os
import shutil
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
METRICS = os.path.join(DATA, "metrics.csv")
STATUS = os.path.join(DATA, "status.json")
STOP = os.path.join(DATA, "STOP")

SIZES_MUSD = [0.1, 0.5, 1, 2, 5, 10]
DEPTH_BANDS_BPS = [5, 10, 25, 50]
RAW_BAND_BPS = 150
MIN_FREE_GB_FOR_RAW = 3.0
TIMEOUT_S = 15
HEADERS = {"User-Agent": "otc-execution-cost/0.1 (research)"}

# (venue, asset) -> (url, params)
BOOKS = {
    ("binance", "BTC"): ("https://api.binance.com/api/v3/depth", {"symbol": "BTCUSDT", "limit": 5000}),
    ("binance", "ETH"): ("https://api.binance.com/api/v3/depth", {"symbol": "ETHUSDT", "limit": 5000}),
    ("coinbase", "BTC"): ("https://api.exchange.coinbase.com/products/BTC-USD/book", {"level": 2}),
    ("coinbase", "ETH"): ("https://api.exchange.coinbase.com/products/ETH-USD/book", {"level": 2}),
    ("kraken", "BTC"): ("https://api.kraken.com/0/public/Depth", {"pair": "XBTUSD", "count": 500}),
    ("kraken", "ETH"): ("https://api.kraken.com/0/public/Depth", {"pair": "ETHUSD", "count": 500}),
}
QUOTE = {"binance": "USDT", "coinbase": "USD", "kraken": "USD"}

session = requests.Session()
session.headers.update(HEADERS)
log = logging.getLogger("collector")


def keep_awake():
    """Ask Windows not to sleep while this process runs (reverts when it exits)."""
    if os.name == "nt":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def parse(venue, payload):
    """Return (bids, asks) as lists of (price, qty) floats, best first."""
    if venue == "kraken":
        if payload.get("error"):
            raise RuntimeError(f"kraken error {payload['error']}")
        book = next(iter(payload["result"].values()))
    else:
        book = payload
    bids = [(float(l[0]), float(l[1])) for l in book["bids"]]
    asks = [(float(l[0]), float(l[1])) for l in book["asks"]]
    bids.sort(key=lambda l: -l[0])
    asks.sort(key=lambda l: l[0])
    return bids, asks


def fetch(key):
    url, params = BOOKS[key]
    last_err = None
    for attempt in range(3):
        t0 = time.time()
        try:
            r = session.get(url, params=params, timeout=TIMEOUT_S)
            if r.status_code in (418, 429):
                raise RuntimeError(f"rate limited {r.status_code} retry-after={r.headers.get('Retry-After')}")
            r.raise_for_status()
            payload = r.json()
            bids, asks = parse(key[0], payload)
            if not bids or not asks:
                raise RuntimeError("empty book")
            return {"key": key, "t_req": t0, "t_resp": time.time(), "bids": bids, "asks": asks,
                    "exch_time": payload.get("time") if isinstance(payload, dict) else None}
        except Exception as e:  # noqa: BLE001 - one bad venue must never stop the others
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{key}: {last_err!r}")


def sweep_cost_bps(levels, mid, notional, side):
    """VWAP slippage vs mid (bps) of sweeping `notional` USD through `levels`. None if not enough depth."""
    remaining, qty_total, cost_total = notional, 0.0, 0.0
    for px, qty in levels:
        take = min(remaining, px * qty)
        qty_total += take / px
        cost_total += take
        remaining -= take
        if remaining <= 1e-9:
            vwap = cost_total / qty_total
            return (vwap - mid) / mid * 1e4 if side == "buy" else (mid - vwap) / mid * 1e4
    return None


def depth_within(levels, mid, bps, side):
    lim = mid * (1 + bps / 1e4) if side == "ask" else mid * (1 - bps / 1e4)
    inside = (lambda p: p <= lim) if side == "ask" else (lambda p: p >= lim)
    return sum(px * q for px, q in levels if inside(px))


def metrics_row(ts, key, snap):
    bids, asks = snap["bids"], snap["asks"]
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2
    row = {
        "ts_utc": ts, "venue": key[0], "asset": key[1], "quote": QUOTE[key[0]],
        "latency_ms": round((snap["t_resp"] - snap["t_req"]) * 1000),
        "mid": mid, "best_bid": bb, "best_ask": ba,
        "spread_bps": round((ba - bb) / mid * 1e4, 4),
        "n_bid": len(bids), "n_ask": len(asks),
        "book_bid_musd": round(sum(p * q for p, q in bids) / 1e6, 3),
        "book_ask_musd": round(sum(p * q for p, q in asks) / 1e6, 3),
    }
    for b in DEPTH_BANDS_BPS:
        row[f"depth_bid_{b}bps_musd"] = round(depth_within(bids, mid, b, "bid") / 1e6, 4)
        row[f"depth_ask_{b}bps_musd"] = round(depth_within(asks, mid, b, "ask") / 1e6, 4)
    for s in SIZES_MUSD:
        buy = sweep_cost_bps(asks, mid, s * 1e6, "buy")
        sell = sweep_cost_bps(bids, mid, s * 1e6, "sell")
        row[f"buy_{s}m_bps"] = None if buy is None else round(buy, 4)
        row[f"sell_{s}m_bps"] = None if sell is None else round(sell, 4)
    return row


def consolidated_row(ts, asset, snaps):
    """Merge the three books in bps-from-own-mid space and sweep the combined liquidity."""
    asks, bids = [], []  # (offset_bps, notional_usd, venue)
    for (venue, a), snap in snaps.items():
        if a != asset:
            continue
        mid = (snap["bids"][0][0] + snap["asks"][0][0]) / 2
        asks += [((p - mid) / mid * 1e4, p * q, venue) for p, q in snap["asks"]]
        bids += [((mid - p) / mid * 1e4, p * q, venue) for p, q in snap["bids"]]
    venues = sorted({v for (v, a) in snaps if a == asset})
    row = {"ts_utc": ts, "venue": "ALL3" if len(venues) == 3 else "+".join(venues), "asset": asset, "quote": "mixed"}
    asks.sort(key=lambda l: l[0])
    bids.sort(key=lambda l: l[0])
    row["spread_bps"] = round(asks[0][0] + bids[0][0], 4)
    for b in DEPTH_BANDS_BPS:
        row[f"depth_bid_{b}bps_musd"] = round(sum(n for o, n, _ in bids if o <= b) / 1e6, 4)
        row[f"depth_ask_{b}bps_musd"] = round(sum(n for o, n, _ in asks if o <= b) / 1e6, 4)
    for side, levels in (("buy", asks), ("sell", bids)):
        for s in SIZES_MUSD:
            remaining, cost, fills = s * 1e6, 0.0, {}
            for off, n, v in levels:
                take = min(remaining, n)
                cost += take * off
                fills[v] = fills.get(v, 0.0) + take
                remaining -= take
                if remaining <= 1e-9:
                    break
            done = remaining <= 1e-9
            row[f"{side}_{s}m_bps"] = round(cost / (s * 1e6), 4) if done else None
            if s == 5 and done:
                for v in ("binance", "coinbase", "kraken"):
                    row[f"{side}_5m_share_{v}"] = round(fills.get(v, 0.0) / 5e6, 4)
    return row


FIELDS = (["ts_utc", "venue", "asset", "quote", "latency_ms", "mid", "best_bid", "best_ask", "spread_bps",
           "n_bid", "n_ask", "book_bid_musd", "book_ask_musd"]
          + [f"depth_{s}_{b}bps_musd" for b in DEPTH_BANDS_BPS for s in ("bid", "ask")]
          + [f"{side}_{s}m_bps" for s in SIZES_MUSD for side in ("buy", "sell")]
          + [f"{side}_5m_share_{v}" for side in ("buy", "sell") for v in ("binance", "coinbase", "kraken")])


def append_metrics(rows):
    new = not os.path.exists(METRICS)
    with open(METRICS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def save_raw(ts_dt, key, snap):
    if shutil.disk_usage(DATA).free / 1e9 < MIN_FREE_GB_FOR_RAW:
        return False
    bids, asks = snap["bids"], snap["asks"]
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * (1 - RAW_BAND_BPS / 1e4), mid * (1 + RAW_BAND_BPS / 1e4)
    rec = {"ts": ts_dt.isoformat(), "t_req": round(snap["t_req"], 3), "t_resp": round(snap["t_resp"], 3),
           "exch_time": snap["exch_time"],
           "bids": [[p, q] for p, q in bids if p >= lo], "asks": [[p, q] for p, q in asks if p <= hi]}
    folder = os.path.join(RAW, ts_dt.strftime("%Y%m%d"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{ts_dt:%H}_{key[0]}_{key[1]}.jsonl.gz")
    with gzip.open(path, "at", encoding="utf-8") as f:  # one gzip member per line: crash-safe append
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return True


def collect_once(pool, ts_dt):
    ts = ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    futures = {pool.submit(fetch, k): k for k in BOOKS}
    snaps, errors = {}, []
    for fut in cf.as_completed(futures):
        try:
            snaps[futures[fut]] = fut.result()
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    rows = [metrics_row(ts, k, s) for k, s in sorted(snaps.items())]
    for asset in ("BTC", "ETH"):
        if sum(1 for (_, a) in snaps if a == asset) >= 2:
            rows.append(consolidated_row(ts, asset, snaps))
    append_metrics(rows)
    raw_ok = all([save_raw(ts_dt, k, s) for k, s in snaps.items()]) if snaps else False
    return len(snaps), errors, raw_ok


def write_status(state):
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATUS)


def main():
    os.makedirs(RAW, exist_ok=True)
    logging.basicConfig(filename=os.path.join(DATA, "collector.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime
    keep_awake()
    log.info("collector start pid=%s", os.getpid())
    state = {"pid": os.getpid(), "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
             "snapshots": 0, "books_ok": 0, "books_failed": 0, "last_ts": None, "last_errors": []}
    max_iter = int(sys.argv[1]) if len(sys.argv) > 1 else None  # for tests: run N minutes then exit
    with cf.ThreadPoolExecutor(max_workers=len(BOOKS)) as pool:
        while not os.path.exists(STOP):
            now = time.time()
            if max_iter is None or state["snapshots"] > 0:
                time.sleep(60 - now % 60)  # wait for the next minute boundary
            ts_dt = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
            try:
                n_ok, errors, raw_ok = collect_once(pool, ts_dt)
                state["snapshots"] += 1
                state["books_ok"] += n_ok
                state["books_failed"] += len(errors)
                state["last_ts"] = ts_dt.isoformat()
                state["raw_saved"] = raw_ok
                state["free_gb"] = round(shutil.disk_usage(DATA).free / 1e9, 2)
                if errors:
                    state["last_errors"] = (state["last_errors"] + [f"{ts_dt:%H:%M} {e}" for e in errors])[-20:]
                    log.warning("%s errors: %s", ts_dt, errors)
                if state["snapshots"] % 30 == 1:
                    log.info("ok %s books=%d raw=%s free=%.1fGB", ts_dt, n_ok, raw_ok, state["free_gb"])
            except Exception:  # noqa: BLE001 - never die on a single bad minute
                log.exception("snapshot failed")
            write_status(state)
            if max_iter is not None and state["snapshots"] >= max_iter:
                break
    log.info("collector stop")


if __name__ == "__main__":
    main()
