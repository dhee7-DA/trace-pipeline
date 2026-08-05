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
from datetime import datetime
from lxml import etree
from edgar_client import company_submissions, load_company_tickers, filing_doc_url, get_text, get_json

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "watchlist_tickers.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "form4.json")
DEBUG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "form4_debug_sample.txt")

MAX_FILINGS_PER_TICKER = 15   # caps ATTEMPTS now, not just successes — see main()
_debug_sample_saved = False   # only save one raw-content sample per run, not one per failure


def local_tag(elem):
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def text_of(root, path):
    el = root.find(path)
    return el.text.strip() if el is not None and el.text else None


def parse_form4(xml_text, source_url):
    """
    Uses lxml with recover=True: SEC Form 4 documents are supposed to be
    clean XML, but in practice some filings (older ones, or ones routed
    through an XSLT-rendering path) come back with malformed or HTML-ish
    markup that Python's stdlib xml.etree rejects outright with
    "mismatched tag". lxml's recovering parser reconstructs a best-effort
    tree instead of aborting, which is the right tradeoff here: we still
    only extract fields that are actually present in the reconstructed
    tree — this doesn't invent data, it just tolerates messy markup.
    """
    parser = etree.XMLParser(recover=True)
    root = etree.fromstring(xml_text.encode("utf-8", errors="replace"), parser=parser)
    if root is None:
        raise ValueError("document could not be recovered as XML at all (root is None)")

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

    if not issuer_name and not owner_name and not transactions:
        # Recovered a tree but found none of the fields we expect — almost
        # certainly not a Form 4 ownership document at all (e.g. an HTML
        # cover page). Treat as a failure rather than emitting an empty record.
        raise ValueError("recovered document has none of the expected Form 4 fields")

    return {
        "issuerTicker": issuer_symbol,
        "issuerName": issuer_name,
        "ownerName": owner_name,
        "role": role,
        "periodOfReport": period_of_report,
        "sourceUrl": source_url,
        "transactions": transactions,
    }


def find_raw_xml_url(cik10, accession_nodashes):
    """
    The `primaryDocument` field from EDGAR's submissions API points at the
    XSL-rendered HTML view (SEC's own naming convention: the xslF345X06/
    subfolder means "rendered via stylesheet X06" — it's HTML despite the
    .xml filename). The real machine-readable ownership XML sits as a
    separate, top-level file in the same accession folder. We fetch the
    folder's own index and pick the .xml file that ISN'T inside an xsl*
    subfolder — that's the actual data file this script needs.
    """
    index = get_json(f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession_nodashes}/index.json")
    items = index.get("directory", {}).get("item", [])
    candidates = [
        it["name"] for it in items
        if it["name"].lower().endswith(".xml") and "/" not in it["name"] and not it["name"].lower().startswith("xsl")
    ]
    if not candidates:
        return None
    # Prefer a name that doesn't look like an index/summary file
    candidates.sort(key=lambda n: ("index" in n.lower(), len(n)))
    return filing_doc_url(cik10, accession_nodashes, candidates[0])


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
        count = 0        # successful parses
        attempted = 0    # total tries — THIS is what caps the loop now
        failures = 0
        for i, form in enumerate(forms):
            if form != "4":
                continue
            if attempted >= MAX_FILINGS_PER_TICKER:
                break
            attempted += 1
            accession = recent["accessionNumber"][i]
            accession_nodashes = accession.replace("-", "")
            filed_date = recent["filingDate"][i]
            try:
                doc_url = find_raw_xml_url(cik10, accession_nodashes)
            except Exception as e:
                failures += 1
                print(f"[warn] could not locate raw XML for {ticker} accession {accession}: {e}")
                continue
            if not doc_url:
                failures += 1
                print(f"[warn] no non-rendered .xml file found for {ticker} accession {accession}")
                continue
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
                failures += 1
                print(f"[warn] failed to parse Form 4 doc for {ticker} ({doc_url}): {e}")
                global _debug_sample_saved
                if not _debug_sample_saved:
                    try:
                        raw = get_text(doc_url)
                        with open(DEBUG_PATH, "w", encoding="utf-8") as dbg:
                            dbg.write(f"URL: {doc_url}\n\n--- first 3000 chars of raw response ---\n\n")
                            dbg.write(raw[:3000])
                        _debug_sample_saved = True
                        print(f"       saved raw content sample to {DEBUG_PATH} for diagnosis")
                    except Exception:
                        pass
        print(f"[{'ok' if count else 'warn'}] {ticker}: {count} parsed, {failures} failed, {attempted} attempted")

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
