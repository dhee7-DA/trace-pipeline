"""
Shared SEC EDGAR HTTP client.

SEC's fair-access rules (https://www.sec.gov/os/accessing-edgar-data):
  - Every request MUST send a descriptive User-Agent: "AppName contact@email.com"
    Requests without one, or with a generic/browser User-Agent, get blocked.
  - Rate limit: stay at or under ~10 requests/second. We throttle much lower
    (2/sec) since a daily batch job has no reason to push the limit.
  - All endpoints below are official, free, and require no API key.

Set EDGAR_CONTACT_EMAIL as a repo secret / env var before running anything —
this becomes part of the User-Agent SEC sees on every request.
"""
import os
import time
import json
import requests

CONTACT_EMAIL = os.environ.get("EDGAR_CONTACT_EMAIL", "").strip()
if not CONTACT_EMAIL:
    raise SystemExit(
        "EDGAR_CONTACT_EMAIL is not set. SEC requires a real contact email in the "
        "User-Agent header on every request — set this as a GitHub Actions secret "
        "(or local env var) before running the pipeline. Example:\n"
        '  export EDGAR_CONTACT_EMAIL="yourname@example.com"'
    )

USER_AGENT = f"TRACE-SmartMoneyTracker/1.0 ({CONTACT_EMAIL})"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

_MIN_INTERVAL = 0.5  # 2 req/sec — well under SEC's 10 req/sec ceiling
_last_request_time = 0.0


def _throttle():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_time = time.time()


def get_json(url, retries=3):
    """GET a URL expected to return JSON, with throttling and retry on 429/5xx."""
    for attempt in range(retries):
        _throttle()
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 503) and attempt < retries - 1:
            time.sleep(2 ** attempt * 2)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts")


def get_text(url, retries=3):
    """GET a URL expected to return raw text/XML, with throttling and retry."""
    for attempt in range(retries):
        _throttle()
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (429, 503) and attempt < retries - 1:
            time.sleep(2 ** attempt * 2)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts")


def company_submissions(cik10):
    """https://data.sec.gov/submissions/CIK##########.json — a filer's recent filings list."""
    return get_json(f"https://data.sec.gov/submissions/CIK{cik10}.json")


def filing_index_url(cik, accession_no_dashes):
    cik_int = int(cik)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/"


def filing_doc_url(cik, accession_no_dashes, filename):
    cik_int = int(cik)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{filename}"


def load_company_tickers():
    """
    https://www.sec.gov/files/company_tickers.json — SEC's own authoritative
    ticker -> CIK map, refreshed by SEC itself. Using this instead of a hand-typed
    mapping is the accurate approach: it's the same file EDGAR's own search uses.
    Returns dict: TICKER -> {"cik": "##########", "title": "..."}
    """
    raw = get_json("https://www.sec.gov/files/company_tickers.json")
    out = {}
    for entry in raw.values():
        ticker = entry["ticker"].upper()
        out[ticker] = {"cik": str(entry["cik_str"]).zfill(10), "title": entry["title"]}
    return out
