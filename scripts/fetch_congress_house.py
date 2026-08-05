"""
Index House financial disclosure filings from disclosures-clerk.house.gov.

Unlike the Senate, House Periodic Transaction Reports are filed as PDFs that
are frequently scanned/image-based, not structured text — there is no
reliable free way to extract ticker/amount/date from an arbitrary House PTR
without OCR, and OCR on financial forms has a real error rate. Rather than
run OCR and risk silently wrong tickers or dollar amounts in a money app,
this script does the honest thing: it indexes WHICH members filed WHAT type
of disclosure WHEN, with a direct link to the actual PDF, and leaves
transaction-level extraction to either (a) manual review via that link, or
(b) a paid vendor (Quiver) that has already solved PDF parsing at scale.

*** READ BEFORE TRUSTING THIS SCRIPT ***
Same caveat as the Senate script: I have no internet access to test this
against the live site. The year-ZIP URL pattern and XML field names below
match the House Clerk's documented bulk-download format as of my training,
but confirm at https://disclosures-clerk.house.gov/FinancialDisclosure
before relying on it, and tell me if the structure has moved.
"""
import io
import os
import zipfile
from datetime import datetime
import requests
import xml.etree.ElementTree as ET

CONTACT_EMAIL = os.environ.get("EDGAR_CONTACT_EMAIL", "trace-app-contact@example.com")
HEADERS = {"User-Agent": f"TRACE-SmartMoneyTracker/1.0 ({CONTACT_EMAIL})"}


def fetch_year_index(year):
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    resp = requests.get(url, headers=HEADERS, timeout=60)
    if resp.status_code != 200:
        print(f"[warn] could not fetch House disclosure index for {year} (HTTP {resp.status_code}) — URL may have moved, check disclosures-clerk.house.gov")
        return []
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
    if not xml_name:
        print(f"[warn] no XML index found inside {year}FD.zip")
        return []
    root = ET.fromstring(zf.read(xml_name))

    filings = []
    for member in root.findall(".//Member"):
        def field(tag):
            el = member.find(tag)
            return el.text.strip() if el is not None and el.text else None

        filing_type = field("FilingType")
        if filing_type != "P":  # 'P' = Periodic Transaction Report per House Clerk's own legend
            continue
        doc_id = field("DocID")
        if not doc_id:
            continue
        filings.append({
            "person": f"{field('First')} {field('Last')}".strip(),
            "state": field("StateDst"),
            "filingDate": field("FilingDate"),
            "year": year,
            "docId": doc_id,
            "pdfUrl": f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf",
            "extractionStatus": "index-only — open pdfUrl to review actual trades; ticker/amount not auto-extracted",
        })
    return filings


def fetch_house_index(years=None):
    if years is None:
        years = [datetime.utcnow().year]
    all_filings = []
    for y in years:
        all_filings.extend(fetch_year_index(y))
    return all_filings


if __name__ == "__main__":
    import json
    data = fetch_house_index()
    print(json.dumps(data[:5], indent=2))
    print(f"\n{len(data)} PTR filings indexed for {datetime.utcnow().year}")
