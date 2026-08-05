"""
Enriches TRACE's trade data with real prices so "since trade" percentages
stop showing "—". Uses Twelve Data's free tier (800 requests/day, ~8/min),
which is the one free provider that covers BOTH what we actually need:
  - the closing price ON the trade date (historical daily bars)
  - today's price (latest quote)

Personal-use note: Twelve Data's free tier is fine for a personal tool like
this. If you ever turn TRACE into something other people use, re-check their
ToS — free-tier terms are usually personal/non-commercial only.

Strategy to stay well inside the free quota:
  - Historical close on a given (ticker, date) NEVER changes once the market
    has closed for that day, so it's cached permanently in
    data/price_cache.json and never re-fetched.
  - Only TODAY's price is fetched fresh every run — one request per distinct
    ticker across all three sources, typically 20-40 requests/day for a
    personal watchlist. Comfortably inside 800/day.

Does not fabricate a price if the API call fails — that ticker/date is left
out of the cache and the frontend shows "—" rather than a wrong number.
"""
import json
import os
import time
from datetime import datetime, timedelta
import requests

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CACHE_PATH = os.path.join(DATA_DIR, "price_cache.json")   # historical closes, permanent
OUT_PATH = os.path.join(DATA_DIR, "prices.json")           # latest quotes, refreshed daily

_last_call = 0.0
def throttle():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < 8:  # ~7.5 req/min, safely under Twelve Data's free-tier ~8/min cap
        time.sleep(8 - elapsed)
    _last_call = time.time()


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def collect_needed_tickers_and_dates():
    """Pull every (ticker, trade date) pair out of the three source files we
    already have, plus the set of distinct tickers needing a current quote."""
    pairs = set()
    tickers = set()

    def add(ticker, date):
        if ticker and date and not ticker.startswith("("):  # skip unresolved "(CUSIP ...)" placeholders
            pairs.add((ticker, date))
            tickers.add(ticker)

    form4_path = os.path.join(DATA_DIR, "form4.json")
    if os.path.exists(form4_path):
        with open(form4_path) as f:
            d = json.load(f)
        for filing in d.get("filings", []):
            for t in filing.get("transactions", []):
                add(filing.get("issuerTicker"), t.get("transactionDate"))

    tf_path = os.path.join(DATA_DIR, "13f.json")
    if os.path.exists(tf_path):
        with open(tf_path) as f:
            d = json.load(f)
        for m in d.get("managers", []):
            for h in m.get("holdings", []):
                add(h.get("ticker"), m.get("filedDate"))

    cong_path = os.path.join(DATA_DIR, "congress.json")
    if os.path.exists(cong_path):
        with open(cong_path) as f:
            d = json.load(f)
        for t in d.get("trades", []):
            add(t.get("ticker"), t.get("tradeDate"))

    return pairs, tickers


def fetch_historical_close(ticker, date):
    """Twelve Data time_series for a single trading day. If the date lands on
    a weekend/holiday, we widen slightly and take the nearest prior close."""
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    throttle()
    resp = requests.get("https://api.twelvedata.com/time_series", params={
        "symbol": ticker, "interval": "1day", "start_date": start, "end_date": date,
        "apikey": API_KEY, "outputsize": 5,
    }, timeout=20)
    data = resp.json()
    values = data.get("values")
    if not values:
        print(f"[warn] no historical close for {ticker} near {date}: {data.get('message', 'unknown error')}")
        return None
    return float(values[0]["close"])  # most recent bar at/before the requested date


def fetch_current_price(ticker):
    throttle()
    resp = requests.get("https://api.twelvedata.com/price", params={"symbol": ticker, "apikey": API_KEY}, timeout=20)
    data = resp.json()
    if "price" not in data:
        print(f"[warn] no current price for {ticker}: {data.get('message', 'unknown error')}")
        return None
    return float(data["price"])


def main():
    if not API_KEY:
        print("[skip] TWELVE_DATA_API_KEY not set — sinceTrade will stay '—' in the app until this is configured.")
        return

    pairs, tickers = collect_needed_tickers_and_dates()
    if not tickers:
        print("[info] No resolved tickers found in form4.json/13f.json/congress.json yet — run those fetchers first.")
        return

    cache = load_cache()
    new_lookups = 0
    for ticker, date in sorted(pairs):
        key = f"{ticker}|{date}"
        if key in cache:
            continue
        price = fetch_historical_close(ticker, date)
        if price is not None:
            cache[key] = price
            new_lookups += 1
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"[ok] historical price cache: {new_lookups} new entries, {len(cache)} total")

    latest = {}
    for ticker in sorted(tickers):
        price = fetch_current_price(ticker)
        if price is not None:
            latest[ticker] = price

    with open(OUT_PATH, "w") as f:
        json.dump({
            "source": "Twelve Data (free tier) — historical closes cached permanently in price_cache.json, current quotes refreshed daily",
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "latest": latest,
            "historicalCache": cache,
        }, f, indent=2)
    print(f"[ok] current quotes: {len(latest)}/{len(tickers)} tickers resolved")
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
