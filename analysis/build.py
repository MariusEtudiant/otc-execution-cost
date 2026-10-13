"""Build the figures, tables and Excel pricer from the snapshots and the 1-minute history.

Two questions, two data sources:

1. What does it cost to hedge a client clip *now*, by sweeping displayed liquidity?
   -> order book snapshots (data/metrics.csv), measured, a few sessions only.
2. How does liquidity move around the clock, and what does a *patient* hedge risk?
   -> six months of 1-minute candles (data/history), every hour of the week.

Patient hedge: execute at a fixed share of market volume (participation rate). The time
needed is T = Q / (p * V), and the inventory left open decreases linearly, so the P&L
standard deviation is sigma_1m * sqrt(T / 3) (Almgren-Chriss, linear trajectory).
"""

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "reports")

SIZES = [0.1, 0.5, 1, 2, 5, 10]
VENUES = ["binance", "coinbase", "kraken", "ALL3"]
LABEL = {"binance": "Binance", "coinbase": "Coinbase", "kraken": "Kraken", "ALL3": "3 venues combined"}
PARTICIPATION = 0.10
Z = 1.65  # one-sided 95%
CLIP_MUSD = 5

# Local business hours in UTC (October: BST and EDT still in force until 25 Oct / 1 Nov).
DESKS = [("Singapore", 1, 9), ("London", 7, 16), ("New York", 13, 21)]

# Reference palette (dataviz skill, light mode).
C = {"binance": "#2a78d6", "coinbase": "#eb6834", "kraken": "#1baf7a", "ALL3": "#eda100",
     "BTC": "#2a78d6", "ETH": "#eb6834", "weekday": "#2a78d6", "weekend": "#eb6834"}
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.titlesize": 11, "axes.titleweight": "bold", "lines.linewidth": 2,
})


# --------------------------------------------------------------------------- snapshots
def load_snapshots():
    m = pd.read_csv(os.path.join(DATA, "metrics.csv"), parse_dates=["ts_utc"])
    for s in SIZES:  # cost of a clip = average of the buy and sell sweeps
        m[f"cost_{s}m"] = m[[f"buy_{s}m_bps", f"sell_{s}m_bps"]].mean(axis=1, skipna=False)
    m["window"] = m["ts_utc"].map(window_of)
    return m


def window_of(ts):
    h = ts.hour + ts.minute / 60
    if h < 7:
        return "Asia"
    if h < 12:
        return "London morning"
    if h < 16:
        return "London + New York"
    if h < 21:
        return "New York afternoon"
    return "Late US"


def snapshot_tables(m):
    curve = (m.groupby(["asset", "venue"])[[f"cost_{s}m" for s in SIZES]].median()
             .rename(columns=lambda c: float(c[5:-1])))
    single = m[m.venue != "ALL3"]
    best_single = (single.groupby(["asset", "ts_utc"])[f"cost_{CLIP_MUSD}m"].min()
                   .rename("best_single").reset_index())
    combined = m[m.venue == "ALL3"][["asset", "ts_utc", "window", f"cost_{CLIP_MUSD}m"]].rename(
        columns={f"cost_{CLIP_MUSD}m": "combined"})
    paired = combined.merge(best_single, on=["asset", "ts_utc"])
    paired["date"] = paired["ts_utc"].dt.strftime("%d %b")
    by_window = (paired.groupby(["asset", "date", "window"])
                 .agg(minutes=("combined", "size"), start=("ts_utc", "min"), end=("ts_utc", "max"),
                      combined=("combined", "median"), best_single=("best_single", "median"))
                 .reset_index().sort_values(["asset", "start"]))
    venue_window = (m[m.venue != "ALL3"].groupby(["asset", "venue", "window"])[f"cost_{CLIP_MUSD}m"]
                    .median().unstack("window"))
    shares = (m[m.venue == "ALL3"].groupby("asset")
              [[f"{side}_5m_share_{v}" for side in ("buy", "sell") for v in ("binance", "coinbase", "kraken")]]
              .median())
    return curve, by_window, venue_window, shares


# --------------------------------------------------------------------------- history
def load_history(asset):
    b = pd.read_parquet(os.path.join(DATA, "history", f"binance_{asset}_1m.parquet")).set_index("ts")
    path = os.path.join(DATA, "history", f"coinbase_{asset}_1m.parquet")
    c = pd.read_parquet(path).set_index("ts") if os.path.exists(path) else None
    df = pd.DataFrame({"close": b["close"], "vol_binance": b["quote_volume"]})
    df["vol_coinbase"] = c["quote_volume"].reindex(df.index).fillna(0) if c is not None else 0.0
    df["vol_usd"] = df["vol_binance"] + df["vol_coinbase"]
    df["ret_bps"] = np.log(df["close"]).diff() * 1e4
    df["date"] = df.index.date
    df["hour"] = df.index.hour
    df["weekend"] = df.index.dayofweek >= 5
    return df


def liquidity_clock(df):
    """Per (weekday/weekend, hour UTC): typical volume per minute and 1-minute volatility."""
    per_hour = df.groupby(["date", "hour"]).agg(
        weekend=("weekend", "first"), vol_per_min=("vol_usd", "mean"),
        vol_binance=("vol_binance", "mean"), vol_coinbase=("vol_coinbase", "mean"),
        sigma_1m=("ret_bps", lambda r: np.sqrt(np.nanmean(r ** 2))))
    clock = per_hour.groupby(["weekend", "hour"]).median()
    clock["days"] = per_hour.groupby(["weekend", "hour"]).size()
    q = CLIP_MUSD * 1e6
    clock["minutes_to_hedge"] = q / (PARTICIPATION * clock["vol_per_min"])
    clock["risk_bps"] = Z * clock["sigma_1m"] * np.sqrt(clock["minutes_to_hedge"] / 3)
    clock["coinbase_share"] = clock["vol_coinbase"] / clock["vol_per_min"]
    return clock


# --------------------------------------------------------------------------- figures
def desk_bands(ax, y_top):
    for i, (name, start, end) in enumerate(DESKS):
        y = y_top * (1.20 - 0.075 * i)
        ax.plot([start, end], [y, y], color=MUTED, lw=3, solid_capstyle="round", clip_on=False)
        ax.text(end + 0.3, y, name, va="center", ha="left", fontsize=8.5, color=INK2, clip_on=False)


def fig_clock(clocks):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharex=True)
    for ax, asset in zip(axes, ("BTC", "ETH")):
        clock = clocks[asset]
        top = 0
        for weekend, name in ((False, "weekday"), (True, "weekend")):
            s = clock.loc[weekend, "vol_per_min"] / 1e6
            x = np.append(s.index.values, 24)
            y = np.append(s.values, s.values[0])
            ax.step(x, y, where="post", color=C[name], label=name.capitalize() + "s")
            top = max(top, y.max())
        ax.set_ylim(0, top * 1.05)
        desk_bands(ax, top)
        ax.set_xlim(0, 24)
        ax.set_xticks(range(0, 25, 3))
        ax.set_xlabel("Hour (UTC)")
        ax.set_title(f"{asset}: median traded volume, M$ per minute", loc="left", pad=48)
        ax.legend(frameon=False, loc="upper left", fontsize=9)
    fig.text(0.01, 0.005, "Binance + Coinbase spot, 1 Apr to 12 Oct 2026. Bars: local business hours of each desk.",
             fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(OUT, "liquidity_clock.png"), dpi=160)
    plt.close(fig)


def fig_hedge(clocks, curve):
    fig, ax = plt.subplots(figsize=(11, 4.4))
    for asset in ("BTC", "ETH"):
        r = clocks[asset].loc[False, "risk_bps"]
        x = np.append(r.index.values, 24)
        y = np.append(r.values, r.values[0])
        ax.step(x, y, where="post", color=C[asset])
        ax.text(24.2, y[-1], f"{asset} patient hedge", va="center", fontsize=9, color=INK)
        sweep = curve.loc[(asset, "ALL3"), float(CLIP_MUSD)]
        ax.axhline(sweep, color=C[asset], lw=1.2, ls=(0, (4, 3)))
        ax.text(24.2, sweep, f"{asset} sweep now ({sweep:.1f})", va="center", fontsize=9, color=INK2)
    ax.set_xlim(0, 24)
    ax.set_ylim(0, None)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xlabel("Hour (UTC), weekdays")
    ax.set_ylabel("bps of notional")
    ax.set_title(f"Hedging a {CLIP_MUSD} M$ clip: sweep the books now, or trade {PARTICIPATION:.0%} of volume "
                 f"and carry the risk", loc="left")
    fig.text(0.01, 0.045,
             f"Patient hedge: 95% one-sided risk of the open inventory, {Z} x sigma_1m x sqrt(T/3), where T is the "
             f"time to trade the clip at {PARTICIPATION:.0%} of Binance + Coinbase volume (six-month medians).",
             fontsize=8, color=MUTED)
    fig.text(0.01, 0.01, "Sweep now: median measured cost of crossing the combined Binance, Coinbase and Kraken books.",
             fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.07, 0.86, 1))
    fig.savefig(os.path.join(OUT, "hedge_by_hour.png"), dpi=160)
    plt.close(fig)


def spread_labels(ys, min_gap):
    """Nudge label positions apart so that no two are closer than min_gap (data units)."""
    order = np.argsort(ys)
    out = np.array(ys, dtype=float)
    for k in range(1, len(order)):
        prev, cur = order[k - 1], order[k]
        out[cur] = max(out[cur], out[prev] + min_gap)
    return out


def fig_curve(curve, n_minutes):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, asset in zip(axes, ("BTC", "ETH")):
        y_max = 15 if asset == "BTC" else 40
        label_y, label_text = [], []
        for v in VENUES:
            y = curve.loc[(asset, v)].values
            ax.plot(SIZES, y, color=C[v], marker="o", ms=4, lw=2.6 if v == "ALL3" else 1.8, clip_on=True)
            off_scale = y[-1] > y_max
            label_y.append(y_max * 0.97 if off_scale else y[-1])
            label_text.append(f"{LABEL[v]} ({y[-1]:.0f} at 10 M$, off scale)" if off_scale else LABEL[v])
        for yl, text in zip(spread_labels(label_y, y_max * 0.06), label_text):
            ax.text(SIZES[-1] + 0.25, yl, text, va="center", fontsize=8.5, color=INK)
        ax.set_xlim(0, 10.2)
        ax.set_ylim(0, y_max)
        ax.set_xticks([1, 2, 5, 10])
        ax.set_xlabel("Clip size (M$)")
        ax.set_ylabel("bps vs mid")
        ax.set_title(f"{asset}: cost of sweeping the displayed book", loc="left")
    fig.text(0.01, 0.005, f"Median over {n_minutes} one-minute snapshots, average of buy and sell. "
                          "Each venue measured against its own mid, so the USDT/USD basis cancels out.",
             fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(os.path.join(OUT, "cost_curve.png"), dpi=160)
    plt.close(fig)


# --------------------------------------------------------------------------- main
def main():
    os.makedirs(OUT, exist_ok=True)
    m = load_snapshots()
    curve, by_window, venue_window, shares = snapshot_tables(m)
    clocks = {a: liquidity_clock(load_history(a)) for a in ("BTC", "ETH")}

    fig_clock(clocks)
    fig_hedge(clocks, curve)
    n_minutes = m["ts_utc"].nunique()
    fig_curve(curve, n_minutes)

    curve.round(2).to_csv(os.path.join(OUT, "cost_curve.csv"))
    by_window.round(2).to_csv(os.path.join(OUT, "sessions_measured.csv"), index=False)
    venue_window.round(2).to_csv(os.path.join(OUT, "venue_by_window.csv"))
    for a, c in clocks.items():
        c.round(4).to_csv(os.path.join(OUT, f"clock_{a}.csv"))

    pd.set_option("display.width", 200)
    print(f"snapshots: {n_minutes} minutes, {m.ts_utc.min()} -> {m.ts_utc.max()}")
    print("\ncost curve (bps)\n", curve.round(2))
    print("\nmeasured 5M by window\n", by_window.round(2).to_string(index=False))
    print("\nvenue 5M by window\n", venue_window.round(2))
    print("\nshares of a combined 5M fill\n", shares.round(2))
    for a, c in clocks.items():
        wd = c.loc[False]
        print(f"\n{a} weekday clock: vol/min M$ min {wd.vol_per_min.min()/1e6:.2f} (h{wd.vol_per_min.idxmin()}) "
              f"max {wd.vol_per_min.max()/1e6:.2f} (h{wd.vol_per_min.idxmax()}); "
              f"weekend median {c.loc[True].vol_per_min.median()/1e6:.2f} vs weekday {wd.vol_per_min.median()/1e6:.2f}")
        print(c[["vol_per_min", "sigma_1m", "minutes_to_hedge", "risk_bps", "coinbase_share", "days"]]
              .assign(vol_per_min=lambda d: d.vol_per_min / 1e6).round(2).to_string())
    return m, curve, by_window, clocks


if __name__ == "__main__":
    main()
