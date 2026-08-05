"""
Orchestrates Congress trade data across three honest confidence tiers, in
priority order — never fabricating a field a tier can't actually support:

  1. "vendor-api"  — Quiver Quantitative, if QUIVER_API_KEY is set. Real
                      structured ticker/amount/date data, paid but reliable.
  2. "scraped"      — Senate eFD PTR tables, parsed directly (see
                      fetch_congress_senate.py). Free, structured, Senate only.
  3. "index-only"   — House Clerk filing index (see fetch_congress_house.py).
                      Free, confirms a filing exists and links the real PDF,
                      but does NOT claim a ticker/amount — those fields are
                      left null rather than guessed.

All three can run in the same pass: Quiver (if configured) covers both
chambers with full detail; the free scrapers fill in whatever Quiver doesn't
cover, each explicitly tagged with its own confidence so the frontend can
render them differently instead of pretending they're equally certain.
"""
import json
import os
from datetime import datetime
import requests

QUIVER_API_KEY = os.environ.get("QUIVER_API_KEY", "").strip()
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "congress.json")


def fetch_from_quiver():
    resp = requests.get(
        "https://api.quiverquant.com/beta/live/congresstrading",
        headers={"Authorization": f"Bearer {QUIVER_API_KEY}"},
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()
    out = []
    for t in raw:
        out.append({
            "confidence": "vendor-api",
            "person": t.get("Representative") or t.get("Senator"),
            "chamber": "Senate" if t.get("Senator") else "House",
            "party": t.get("Party"),
            "ticker": t.get("Ticker"),
            "transactionType": t.get("Transaction"),
            "amountRange": t.get("Range"),
            "tradeDate": t.get("TransactionDate"),
            "disclosedDate": t.get("ReportDate") or t.get("Filed"),
            "sourceUrl": None,
        })
    return out


def fetch_from_senate_scraper():
    try:
        from fetch_congress_senate import fetch_senate_ptrs
    except ImportError as e:
        print(f"[warn] Senate scraper unavailable ({e}) — is beautifulsoup4 installed?")
        return []
    try:
        filings = fetch_senate_ptrs(days_back=14)
    except Exception as e:
        print(f"[warn] Senate eFD scrape failed: {e}")
        print("       This is the piece I could not test live — see fetch_congress_senate.py's header comment.")
        return []
    out = []
    for f in filings:
        for t in f["transactions"]:
            out.append({
                "confidence": "scraped",
                "person": f["filerName"],
                "chamber": "Senate",
                "party": None,
                "ticker": t["ticker"],
                "transactionType": t["transactionType"],
                "amountRange": t["amountRange"],
                "tradeDate": t["transactionDate"],
                "disclosedDate": None,
                "sourceUrl": f["sourceUrl"],
            })
    return out


def fetch_from_house_index():
    try:
        from fetch_congress_house import fetch_house_index
    except ImportError as e:
        print(f"[warn] House indexer unavailable ({e})")
        return []
    try:
        filings = fetch_house_index()
    except Exception as e:
        print(f"[warn] House Clerk index fetch failed: {e}")
        return []
    out = []
    for f in filings:
        out.append({
            "confidence": "index-only",
            "person": f["person"],
            "chamber": "House",
            "party": None,
            "ticker": None,
            "transactionType": None,
            "amountRange": None,
            "tradeDate": None,
            "disclosedDate": f["filingDate"],
            "sourceUrl": f["pdfUrl"],
        })
    return out


def main():
    trades = []
    if QUIVER_API_KEY:
        try:
            trades.extend(fetch_from_quiver())
            print(f"[ok] Quiver: {len(trades)} trades (vendor-api)")
        except Exception as e:
            print(f"[warn] Quiver fetch failed: {e}")
    else:
        print("[info] QUIVER_API_KEY not set — using free tiers only (lower coverage/detail)")

    senate_trades = fetch_from_senate_scraper()
    print(f"[{'ok' if senate_trades else 'info'}] Senate eFD scrape: {len(senate_trades)} transactions (scraped)")
    trades.extend(senate_trades)

    house_filings = fetch_from_house_index()
    print(f"[{'ok' if house_filings else 'info'}] House Clerk index: {len(house_filings)} filings (index-only)")
    trades.extend(house_filings)

    if not trades:
        print("\n[skip] No data from any tier — leaving data/congress.json unchanged rather than writing an empty/fake result.")
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({
            "source": "Tiered: Quiver Quantitative (vendor-api) + Senate eFD (scraped) + House Clerk index (index-only)",
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "note": "Each trade carries its own 'confidence' field — vendor-api and scraped trades have real ticker/amount; index-only trades confirm a filing exists and link the source PDF but do not claim ticker/amount, which would require OCR this pipeline deliberately does not perform.",
            "trades": trades,
        }, f, indent=2)
    print(f"\nWrote {OUT_PATH} ({len(trades)} total records across all tiers)")


if __name__ == "__main__":
    main()
