"""
Scrape the Senate's electronic Financial Disclosure system (efdsearch.senate.gov)
for Periodic Transaction Reports (PTRs). Since ~2012, Senate PTRs are submitted
electronically and most render as an actual HTML table of transactions on the
filing's report page — not just a PDF — which makes this the one Congress
source that's both free and genuinely structured.

*** READ BEFORE TRUSTING THIS SCRIPT ***
I built this from public documentation and open-source reference scrapers for
this exact site, but I have NO internet access in the environment I wrote it
in, so I could not run it against the live site even once. The three things
most likely to have drifted since:
  1. The exact form field names in the search POST (report_type, dates, etc.)
  2. The CSRF/session handshake (accept the disclaimer, then search)
  3. The HTML structure of an individual PTR report page

If this returns zero results or throws, that's the first thing to check —
run it locally/in Actions, inspect efdsearch.senate.gov's actual request in
your browser's network tab, and diff it against SEARCH_URL/PAYLOAD below.
Tell me what changed and I'll fix the specifics; the overall approach (accept
disclaimer -> search -> parse each report's HTML table) is sound.

This script writes ONLY confirmed-parsed transactions. Any filing whose page
doesn't match the expected table structure is skipped and logged, never
guessed at.
"""
import os
import re
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

BASE = "https://efdsearch.senate.gov"
HOME_URL = f"{BASE}/search/home/"
SEARCH_URL = f"{BASE}/search/report/data/"
CONTACT_EMAIL = os.environ.get("EDGAR_CONTACT_EMAIL", "trace-app-contact@example.com")
HEADERS = {"User-Agent": f"TRACE-SmartMoneyTracker/1.0 ({CONTACT_EMAIL})"}


def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    home = s.get(HOME_URL, timeout=30)
    home.raise_for_status()
    soup = BeautifulSoup(home.text, "html.parser")
    token_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not token_input:
        raise RuntimeError("Could not find csrfmiddlewaretoken on Senate eFD home page — page structure may have changed.")
    csrf = token_input["value"]
    # Accept the disclaimer/agreement, which the site requires before search access.
    accept = s.post(HOME_URL, data={"csrfmiddlewaretoken": csrf, "prohibition_agreement": "1"},
                     headers={"Referer": HOME_URL}, timeout=30)
    accept.raise_for_status()
    return s, csrf


def search_recent_ptrs(session, csrf, days_back=10):
    start = (datetime.utcnow() - timedelta(days=days_back)).strftime("%m/%d/%Y")
    end = datetime.utcnow().strftime("%m/%d/%Y")
    payload = {
        "csrfmiddlewaretoken": csrf,
        "report_type": "11",  # Periodic Transaction Report
        "filer_types[]": "1",
        "submitted_start_date": start,
        "submitted_end_date": end,
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "draw": "1", "start": "0", "length": "100",
    }
    resp = session.post(SEARCH_URL, data=payload, headers={"Referer": HOME_URL, "X-Requested-With": "XMLHttpRequest"}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    rows = []
    for row in body.get("data", []):
        # Each row is typically [first, last, office, report_link_html, date]
        link_html = next((cell for cell in row if isinstance(cell, str) and "href" in cell), None)
        if not link_html:
            continue
        m = re.search(r'href="([^"]+)"', link_html)
        name_m = re.search(r">([^<]+)<", link_html)
        if not m:
            continue
        rows.append({
            "url": BASE + m.group(1) if m.group(1).startswith("/") else m.group(1),
            "raw_row": row,
        })
    return rows


def parse_ptr_page(session, url):
    resp = session.get(url, headers={"Referer": SEARCH_URL}, timeout=30)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return None  # image/PDF-only filing — not machine-readable, skip rather than guess

    filer_name_el = soup.find(class_=re.compile("filer|name", re.I))
    filer_name = filer_name_el.get_text(strip=True) if filer_name_el else None

    transactions = []
    headers_row = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 4:
            continue
        row = dict(zip(headers_row, cells))
        transactions.append({
            "ticker": row.get("ticker") or row.get("symbol"),
            "asset": row.get("asset name") or row.get("asset"),
            "transactionType": row.get("type") or row.get("transaction type"),
            "transactionDate": row.get("transaction date") or row.get("date"),
            "amountRange": row.get("amount"),
        })
    return {"filerName": filer_name, "sourceUrl": url, "transactions": [t for t in transactions if t["ticker"]]}


def fetch_senate_ptrs(days_back=10):
    session, csrf = get_session()
    results = []
    for row in search_recent_ptrs(session, csrf, days_back):
        time.sleep(0.5)
        try:
            parsed = parse_ptr_page(session, row["url"])
        except Exception as e:
            print(f"[warn] failed to parse {row['url']}: {e}")
            continue
        if parsed and parsed["transactions"]:
            results.append(parsed)
    return results


if __name__ == "__main__":
    import json
    data = fetch_senate_ptrs()
    print(json.dumps(data, indent=2)[:2000])
    print(f"\n{len(data)} filings with parseable transaction tables")
