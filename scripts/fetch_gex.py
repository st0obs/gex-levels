"""
fetch_gex.py — Pulls options chains from Massive (Polygon) API,
calculates GEX, put/call walls, max pain, gamma flip for each ticker.
Writes Pine Script-compatible CSVs to data/ folder.

Runs via GitHub Action pre-market, or locally: python3 scripts/fetch_gex.py
"""
import os
import sys
import json
import time
import math
import csv
from datetime import datetime, timedelta
from pathlib import Path
import requests

API_KEY = os.environ.get("MASSIVE_API_KEY", "")
BASE_URL = "https://api.polygon.io"
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Rate limiting: Polygon starter = 5 req/min, Options Starter = higher
RATE_LIMIT_DELAY = 0.25  # seconds between requests — adjust if throttled


def get_top_tickers(n=1000):
    """Get top N most liquid optionable US stocks by volume."""
    tickers = []
    url = f"{BASE_URL}/v3/reference/tickers"
    params = {
        "type": "CS",  # common stock
        "market": "stocks",
        "active": "true",
        "order": "desc",
        "limit": 250,
        "sort": "ticker",
        "apiKey": API_KEY,
    }

    # Polygon doesn't sort by volume in reference endpoint,
    # so we pull a broad set and then rank by snapshot volume.
    # Strategy: get all active tickers first, then get snapshots to rank.

    # Step 1: Get all active stock tickers
    all_tickers = []
    next_url = None
    pages = 0
    while pages < 20:  # safety cap
        if next_url:
            r = requests.get(next_url + f"&apiKey={API_KEY}")
        else:
            r = requests.get(url, params=params)
        if r.status_code != 200:
            print(f"  Ticker list error: {r.status_code} {r.text[:200]}")
            break
        data = r.json()
        results = data.get("results", [])
        all_tickers.extend([t["ticker"] for t in results])
        next_url = data.get("next_url")
        pages += 1
        if not next_url:
            break
        time.sleep(RATE_LIMIT_DELAY)

    print(f"  Found {len(all_tickers)} active tickers")

    # Step 2: Get grouped daily bars (previous day) to rank by volume
    # This is a single API call that returns all tickers
    r = requests.get(
        f"{BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/2026-04-15",
        params={"adjusted": "true", "apiKey": API_KEY}
    )
    if r.status_code != 200:
        print(f"  Grouped daily error: {r.status_code}")
        # Fallback: use a hardcoded list of liquid names
        return _fallback_tickers(n)

    bars = r.json().get("results", [])
    # Filter to optionable-likely (price > $5, volume > 500k)
    ranked = sorted(
        [b for b in bars if b.get("v", 0) > 500000 and b.get("c", 0) > 5],
        key=lambda x: x["v"],
        reverse=True
    )
    tickers = [b["T"] for b in ranked[:n]]
    print(f"  Top {len(tickers)} by volume selected")
    return tickers


def _fallback_tickers(n):
    """Hardcoded fallback of ~200 most commonly traded names."""
    core = [
        "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD", "TSLA",
        "AMZN", "GOOGL", "GOOG", "META", "NFLX", "AVGO", "CRM", "ORCL",
        "ADBE", "INTC", "QCOM", "MU", "AMAT", "LRCX", "KLAC", "MRVL",
        "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "COF",
        "XOM", "CVX", "OXY", "SLB", "COP", "MPC", "VLO", "PSX",
        "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "BMY", "AMGN",
        "HD", "LOW", "TGT", "WMT", "COST", "SBUX", "MCD", "NKE",
        "DIS", "CMCSA", "T", "VZ", "TMUS",
        "BA", "CAT", "DE", "GE", "HON", "UPS", "FDX",
        "COIN", "MSTR", "SQ", "SHOP", "SNOW", "CRWD", "NET", "DDOG",
        "UBER", "ABNB", "RIVN", "LCID", "NIO", "PLTR", "SOFI", "HOOD",
        "SMCI", "ARM", "PANW", "ZS", "FTNT", "OKTA",
        "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP", "XLY", "XLC",
        "GLD", "SLV", "USO", "TLT", "HYG", "EEM", "VXX",
    ]
    return core[:n]


def get_options_chain(ticker):
    """Fetch full options chain snapshot for a ticker from Massive/Polygon."""
    all_contracts = []
    url = f"{BASE_URL}/v3/snapshot/options/{ticker}"
    params = {"limit": 250, "apiKey": API_KEY}

    pages = 0
    while pages < 20:  # safety — most chains are < 5 pages
        r = requests.get(url, params=params)
        if r.status_code == 403:
            return None  # no options access or ticker not optionable
        if r.status_code == 429:
            print(f"    Rate limited, sleeping 60s...")
            time.sleep(60)
            continue
        if r.status_code != 200:
            return None

        data = r.json()
        results = data.get("results", [])
        all_contracts.extend(results)

        next_url = data.get("next_url")
        if not next_url:
            break
        url = next_url
        params = {"apiKey": API_KEY}
        pages += 1
        time.sleep(RATE_LIMIT_DELAY)

    return all_contracts if all_contracts else None


def calc_gex_levels(contracts, spot_price):
    """
    Calculate GEX per strike, put wall, call wall, max pain, gamma flip.

    Returns dict with all levels.
    """
    strikes = {}

    for c in contracts:
        details = c.get("details", {})
        greeks = c.get("greeks", {})
        oi = c.get("open_interest", 0)
        strike = details.get("strike_price")
        contract_type = details.get("contract_type", "").lower()
        gamma = greeks.get("gamma", 0)

        if not strike or not oi or not gamma:
            continue

        if strike not in strikes:
            strikes[strike] = {
                "call_oi": 0, "put_oi": 0,
                "call_gex": 0, "put_gex": 0,
                "call_volume": 0, "put_volume": 0,
            }

        # GEX = gamma * OI * 100 * spot^2 * 0.01
        gex = gamma * oi * 100 * spot_price * spot_price * 0.01

        day = c.get("day", {})
        vol = day.get("volume", 0) or 0

        if contract_type == "call":
            strikes[strike]["call_oi"] += oi
            strikes[strike]["call_gex"] += gex  # positive for calls
            strikes[strike]["call_volume"] += vol
        elif contract_type == "put":
            strikes[strike]["put_oi"] += oi
            strikes[strike]["put_gex"] -= gex  # negative for puts (dealers short puts)
            strikes[strike]["put_volume"] += vol

    if not strikes:
        return None

    # Net GEX per strike
    for s in strikes:
        strikes[s]["net_gex"] = strikes[s]["call_gex"] + strikes[s]["put_gex"]
        strikes[s]["total_oi"] = strikes[s]["call_oi"] + strikes[s]["put_oi"]

    sorted_strikes = sorted(strikes.keys())

    # Call Wall = strike with highest positive call GEX above spot
    call_wall = max(
        [(s, strikes[s]["call_gex"]) for s in sorted_strikes if s >= spot_price and strikes[s]["call_gex"] > 0],
        key=lambda x: x[1], default=(None, 0)
    )[0]

    # Put Wall = strike with most negative put GEX below spot
    put_wall = min(
        [(s, strikes[s]["put_gex"]) for s in sorted_strikes if s <= spot_price and strikes[s]["put_gex"] < 0],
        key=lambda x: x[1], default=(None, 0)
    )[0]

    # Gamma Flip = strike where net GEX crosses zero nearest to spot
    gamma_flip = None
    min_dist = float("inf")
    for i in range(len(sorted_strikes) - 1):
        s1, s2 = sorted_strikes[i], sorted_strikes[i + 1]
        g1, g2 = strikes[s1]["net_gex"], strikes[s2]["net_gex"]
        if g1 * g2 < 0:  # sign change
            # Interpolate
            flip = s1 + (s2 - s1) * abs(g1) / (abs(g1) + abs(g2))
            dist = abs(flip - spot_price)
            if dist < min_dist:
                min_dist = dist
                gamma_flip = round(flip, 2)

    # Max Pain = strike where total dollar loss to option holders is maximized
    # (i.e., where OI * intrinsic value summed across all strikes is minimized)
    max_pain = None
    min_pain_val = float("inf")
    for test_price in sorted_strikes:
        total_pain = 0
        for s in sorted_strikes:
            if s < test_price:
                total_pain += strikes[s]["call_oi"] * (test_price - s) * 100
            elif s > test_price:
                total_pain += strikes[s]["put_oi"] * (s - test_price) * 100
        if total_pain < min_pain_val:
            min_pain_val = total_pain
            max_pain = test_price

    # Top 5 strikes by absolute net GEX (key levels)
    top_gex = sorted(
        [(s, strikes[s]["net_gex"], strikes[s]["call_oi"], strikes[s]["put_oi"])
         for s in sorted_strikes],
        key=lambda x: abs(x[1]),
        reverse=True
    )[:5]

    # HVL = highest absolute GEX strike
    hvl = top_gex[0][0] if top_gex else None

    return {
        "spot": spot_price,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": gamma_flip,
        "max_pain": max_pain,
        "hvl": hvl,
        "top_gex": top_gex,
        "strikes": strikes,
    }


def write_csv(ticker, levels):
    """
    Write a Pine Script request.seed()-compatible CSV.

    Format: time,call_wall,put_wall,gamma_flip,max_pain,hvl,gex1,gex2,gex3,gex4,gex5
    Pine's request.seed() expects: first column = time (unix or YYYY-MM-DD),
    remaining columns = float values.
    """
    filepath = DATA_DIR / f"{ticker}.csv"
    today = datetime.now().strftime("%Y-%m-%d")

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "time", "call_wall", "put_wall", "gamma_flip", "max_pain", "hvl",
            "gex1_strike", "gex2_strike", "gex3_strike", "gex4_strike", "gex5_strike",
        ])
        writer.writerow([
            today,
            levels["call_wall"] or 0,
            levels["put_wall"] or 0,
            levels["gamma_flip"] or 0,
            levels["max_pain"] or 0,
            levels["hvl"] or 0,
        ] + [g[0] for g in levels["top_gex"]]
          + [0] * (5 - len(levels["top_gex"]))  # pad to 5
        )

    return filepath


def get_spot_price(ticker):
    """Get last closing price for a ticker."""
    r = requests.get(
        f"{BASE_URL}/v2/aggs/ticker/{ticker}/prev",
        params={"adjusted": "true", "apiKey": API_KEY}
    )
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0].get("c")
    return None


def main():
    if not API_KEY:
        print("ERROR: Set MASSIVE_API_KEY environment variable")
        sys.exit(1)

    print(f"GEX Level Calculator — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # Get top tickers
    print("\n[1/3] Getting top liquid tickers...")
    tickers = get_top_tickers(1000)

    print(f"\n[2/3] Fetching options chains and calculating GEX...")
    success = 0
    errors = 0
    skipped = 0

    for i, ticker in enumerate(tickers):
        pct = (i + 1) / len(tickers) * 100
        sys.stdout.write(f"\r  [{i+1}/{len(tickers)}] ({pct:.0f}%) {ticker:<8} ")
        sys.stdout.flush()

        try:
            spot = get_spot_price(ticker)
            if not spot:
                skipped += 1
                continue

            chain = get_options_chain(ticker)
            if not chain:
                skipped += 1
                continue

            levels = calc_gex_levels(chain, spot)
            if not levels:
                skipped += 1
                continue

            write_csv(ticker, levels)
            success += 1
            time.sleep(RATE_LIMIT_DELAY)

        except Exception as e:
            print(f"\n    ERROR on {ticker}: {e}")
            errors += 1
            time.sleep(1)

    print(f"\n\n[3/3] Done!")
    print(f"  Success: {success}")
    print(f"  Skipped: {skipped}")
    print(f"  Errors:  {errors}")
    print(f"  CSVs in: {DATA_DIR}")


if __name__ == "__main__":
    main()
