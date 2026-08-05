"""
Pull recent Form 4 (insider transaction) filings for each ticker in
config/watchlist_tickers.json, parse the real transaction XML, and write
normalized JSON to data/form4.json.

Form 4 is the freshest and most reliable of the three data sources this
pipeline covers: filers must submit within 2 business days of the trade, and
EDGAR typically has it live within minutes of submission. We record both
the transaction date and the filing date so the frontend can show the true
(usually small) lag rather than implying same-second accuracy.

We deliberately do NOT invent a personal name for the "person" field beyond
what SEC discloses in the filing itself (reportingOwner name + officer title)
— that's exactly what EDGAR gives us, so no fabrication risk here; this is
the one place in the whole app where every field is a real filed fact.
"""
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from edgar_client import company_submissions, load_company_tickers, filing_doc_url, get_text

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "watchlist_tickers.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "form4.json")

MAX_FILINGS_PER_TICKER = 15


def local_tag(elem):
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def text_of(root, path):
    el = root.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(xml_text, source_url):
    root = ET.fromstring(xml_text)
    issuer_symbol = text_of(root, ".//issuer/issuerTradingSymbol")
    issuer_name = text_of(root, ".//issuer/issuerName")
    period_of_report = text_of(root, ".//periodOfReport")

    owner_name = text_of(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    role_bits = []
    if rel is not None:
        if text_of(rel, "isDirector") == "1":
            role_bits.append("Director")
        if text_of(rel, "isOfficer") == "1":
            title = text_of(rel, "officerTitle")
            role_bits.append(title if title else "Officer")
        if text_of(rel, "isTenPercentOwner") == "1":
            role_bits.append("10% Owner")
    role = ", ".join(role_bits) if role_bits else "Reporting Person"

    transactions = []
    for txn in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = text_of(txn, "transactionCoding/transactionCode")
        date = text_of(txn, "transactionDate/value") or period_of_report
        shares = text_of(txn, "transactionAmounts/transactionShares/value")
        price = text_of(txn, "transactionAmounts/transactionPricePerShare/value")
        acq_disp = text_of(txn, "transactionAmounts/transactionAcquiredDisposedCode/value")
        transactions.append({
            "transactionCode": code,  # SEC codes: P=open mkt buy, S=open mkt sell, A=grant, M=option exercise, etc.
            "transactionDate": date,
            "shares": float(shares) if shares else None,
            "pricePerShare": float(price) if price else None,
            "acquiredOrDisposed": "Acquired" if acq_disp == "A" else "Disposed" if acq_disp == "D" else None,
        })

    return {
        "issuerTicker": issuer_symbol,
        "issuerName": issuer_name,
        "ownerName": owner_name,
        "role": role,
        "periodOfReport": period_of_report,
        "sourceUrl": source_url,
        "transactions": transactions,
    }


def main():
    with open(CONFIG_PATH) as f:
        watchlist = json.load(f)["tickers"]

    print("Resolving tickers against SEC's authoritative company_tickers.json...")
    ticker_map = load_company_tickers()

    all_filings = []
    for ticker in watchlist:
        entry = ticker_map.get(ticker.upper())
        if not entry:
            print(f"[warn] {ticker} not found in SEC's company_tickers.json — skipping")
            continue
        cik10 = entry["cik"]
        try:
            subs = company_submissions(cik10)
        except Exception as e:
            print(f"[warn] failed to fetch submissions for {ticker} (CIK {cik10}): {e}")
            continue

        recent = subs.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        count = 0
        for i, form in enumerate(forms):
            if form != "4":
                continue
            if count >= MAX_FILINGS_PER_TICKER:
                break
            accession = recent["accessionNumber"][i]
            accession_nodashes = accession.replace("-", "")
            primary_doc = recent["primaryDocument"][i]
            filed_date = recent["filingDate"][i]
            doc_url = filing_doc_url(cik10, accession_nodashes, primary_doc)
            try:
                xml_text = get_text(doc_url)
                parsed = parse_form4(xml_text, source_url=f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession_nodashes}/")
                parsed["filedDate"] = filed_date
                parsed["accessionNumber"] = accession
                # lag: days between the actual transaction and the SEC filing date
                if parsed["transactions"] and parsed["transactions"][0]["transactionDate"]:
                    try:
                        t_date = datetime.strptime(parsed["transactions"][0]["transactionDate"], "%Y-%m-%d")
                        f_date = datetime.strptime(filed_date, "%Y-%m-%d")
                        parsed["filingLagDays"] = (f_date - t_date).days
                    except ValueError:
                        parsed["filingLagDays"] = None
                all_filings.append(parsed)
                count += 1
            except Exception as e:
                print(f"[warn] failed to parse Form 4 doc for {ticker}: {e}")
        print(f"[ok] {ticker}: {count} Form 4 filings")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({
            "source": "SEC EDGAR Form 4 filings (data.sec.gov)",
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "note": "Filers must submit within 2 business days of the transaction — filingLagDays should almost always be 0-2.",
            "filings": all_filings,
        }, f, indent=2)
    print(f"\nWrote {OUT_PATH} ({len(all_filings)} filings)")


if __name__ == "__main__":
    main()
