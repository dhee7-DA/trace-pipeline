"""
Pull the most recent 13F-HR (institutional holdings) filing for each verified
manager in config/managers.json, parse the real information table, and write
normalized JSON to data/13f.json.

IMPORTANT ACCURACY NOTES (surface these in the UI, don't just bury them here):
  - A 13F-HR reports positions as of a quarter-end "period of report" date,
    but isn't filed until up to 45 days AFTER that date. A filing that just
    posted today can describe a position that's 6+ weeks stale. We record
    both `periodOfReport` and `filedDate` so the frontend can show the true lag.
  - 13F only covers US-listed long equity positions above reporting thresholds.
    It omits short positions, most options, and non-US holdings entirely.
    A manager's real portfolio is NOT fully represented by their 13F.
  - We diff against the previous run's data.json (if present) to classify each
    holding as New Position / Increased / Decreased / Sold Out — this mirrors
    what the frontend expects instead of raw share counts.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from edgar_client import company_submissions, get_json, get_text, filing_doc_url, load_company_tickers

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "managers.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "13f.json")


def normalize_company_name(name):
    """Strip legal suffixes/punctuation so issuer names from 13F filings can be
    matched against SEC's own company_tickers.json titles without guessing."""
    if not name:
        return ""
    n = name.upper()
    for junk in [",", ".", "'", "-", "(", ")"]:
        n = n.replace(junk, "")
    suffixes = [
        " INC", " INCORPORATED", " CORP", " CORPORATION", " CO", " COMPANY",
        " LTD", " LIMITED", " PLC", " LP", " LLC", " HOLDINGS", " HOLDING",
        " GROUP", " CLASS A", " CLASS B", " CL A", " CL B", " COM", " NEW",
        " SPONSORED ADR", " ADR",
    ]
    n = " " + n + " "
    for s in suffixes:
        n = n.replace(s, " ")
    return " ".join(n.split())


def build_name_index(ticker_map):
    """ticker_map: TICKER -> {cik, title}. Returns normalized-name -> [(ticker,title)]."""
    index = {}
    for ticker, info in ticker_map.items():
        key = normalize_company_name(info["title"])
        index.setdefault(key, []).append((ticker, info["title"]))
    return index


def resolve_ticker(issuer_name, name_index):
    """
    Exact normalized-name match only — no fuzzy tier. A fuzzy match against a
    13F issuer's free-text name is exactly the kind of plausible-but-wrong
    answer this pipeline exists to avoid. Anything short of an unambiguous
    exact match is reported by CUSIP instead of a guessed ticker.
    """
    key = normalize_company_name(issuer_name)
    matches = name_index.get(key)
    if matches and len(matches) == 1:
        return matches[0][0], "exact"
    return None, "none"


def local_tag(elem):
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def find_latest_13f(cik10):
    subs = company_submissions(cik10)
    recent = subs["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            return {
                "accessionNumber": recent["accessionNumber"][i],
                "filingDate": recent["filingDate"][i],
                "reportDate": recent["reportDate"][i],  # period of report (quarter-end)
                "primaryDocument": recent["primaryDocument"][i],
            }
    return None


def find_info_table_url(cik10, accession):
    accession_nodashes = accession.replace("-", "")
    index = get_json(f"https://data.sec.gov/Archives/edgar/data/{int(cik10)}/{accession_nodashes}/index.json")
    items = index.get("directory", {}).get("item", [])
    candidates = [it["name"] for it in items if "infotable" in it["name"].lower()]
    if not candidates:
        # fall back to any non-primary XML in the filing
        candidates = [it["name"] for it in items if it["name"].lower().endswith(".xml")]
    if not candidates:
        return None
    return filing_doc_url(cik10, accession_nodashes, candidates[0])


def parse_info_table(xml_text):
    root = ET.fromstring(xml_text)
    holdings = []
    for info_table in root.iter():
        if local_tag(info_table) != "infoTable":
            continue
        row = {}
        for child in info_table:
            row[local_tag(child)] = child.text.strip() if child.text else None
        holdings.append({
            "issuer": row.get("nameOfIssuer"),
            "cusip": row.get("cusip"),
            "value_thousands_usd": int(row["value"]) if row.get("value", "").isdigit() else None,
            "shares": row.get("sshPrnamt"),
        })
    return holdings


def load_previous():
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH) as f:
            return json.load(f)
    return {"managers": []}


def diff_holdings(prev_holdings_by_cusip, current_holdings, name_index):
    out = []
    seen_cusips = set()
    for h in current_holdings:
        cusip = h["cusip"]
        seen_cusips.add(cusip)
        ticker, confidence = resolve_ticker(h["issuer"], name_index)
        prev = prev_holdings_by_cusip.get(cusip)
        if prev is None:
            action = "New Position"
        else:
            prev_val = prev.get("value_thousands_usd") or 0
            cur_val = h.get("value_thousands_usd") or 0
            if cur_val > prev_val * 1.02:
                action = "Increased"
            elif cur_val < prev_val * 0.98:
                action = "Decreased"
            else:
                action = "Held Steady"
        out.append({**h, "action": action, "ticker": ticker, "tickerConfidence": confidence})
    for cusip, prev in prev_holdings_by_cusip.items():
        if cusip not in seen_cusips:
            out.append({**prev, "action": "Sold Out", "value_thousands_usd": 0})
    return out


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    print("Building issuer-name -> ticker index from SEC's company_tickers.json...")
    name_index = build_name_index(load_company_tickers())

    prev_data = load_previous()
    prev_by_manager = {m["display_name"]: m for m in prev_data.get("managers", [])}

    results = []
    skipped = []
    for m in config["managers"]:
        if not m.get("verified") or not m.get("cik"):
            skipped.append(m["display_name"])
            continue
        cik10 = str(m["cik"]).zfill(10)
        latest = find_latest_13f(cik10)
        if not latest:
            print(f"[warn] no 13F-HR found for {m['display_name']} (CIK {cik10})")
            continue
        info_url = find_info_table_url(cik10, latest["accessionNumber"])
        if not info_url:
            print(f"[warn] could not locate information table for {m['display_name']}")
            continue
        holdings = parse_info_table(get_text(info_url))

        prev_manager = prev_by_manager.get(m["display_name"], {})
        prev_by_cusip = {h["cusip"]: h for h in prev_manager.get("holdings", [])}
        diffed = diff_holdings(prev_by_cusip, holdings, name_index)

        resolved = sum(1 for h in diffed if h.get("tickerConfidence") == "exact")
        results.append({
            "display_name": m["display_name"],
            "firm": m["firm"],
            "cik": cik10,
            "accessionNumber": latest["accessionNumber"],
            "filedDate": latest["filingDate"],
            "periodOfReport": latest["reportDate"],
            "filingUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik10}&type=13F-HR",
            "holdings": diffed,
        })
        print(f"[ok] {m['display_name']}: {len(diffed)} holdings, {resolved} ticker-resolved (period {latest['reportDate']}, filed {latest['filingDate']})")

    if skipped:
        print(f"\n[skipped, unverified CIK] {', '.join(skipped)}")
        print("Run scripts/resolve_cik.py for each and set verified:true in config/managers.json")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({
            "source": "SEC EDGAR 13F-HR filings (data.sec.gov)",
            "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "note": "13F holdings reflect a quarter-end period of report and are filed up to 45 days later — see filedDate vs periodOfReport per manager.",
            "managers": results,
        }, f, indent=2)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
