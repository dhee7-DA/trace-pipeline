"""
Look up a fund manager's CIK directly from EDGAR's own company search — the
authoritative source — instead of trusting a hardcoded number from memory.

Usage:
    python resolve_cik.py "Berkshire Hathaway"
    python resolve_cik.py "Scion Asset Management"

Prints candidate CIKs with their registered name so you can confirm the right
one and paste it into config/managers.json.

Implementation note: the CIK is pulled from each atom <entry>'s <link href>,
not from the <title> text. An earlier version of this script assumed the
title always looked like "CIK#0001234567: COMPANY NAME" — that format only
appears on EDGAR's single-company lookup page, not on a multi-result name
search like this one, so it silently matched nothing. The href always
contains CIK=########## regardless of which search produced it, so parsing
that instead is the reliable approach.
"""
import re
import sys
import xml.etree.ElementTree as ET
from edgar_client import get_text
import requests


def search_company(name):
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&company={requests.utils.quote(name)}"
        "&type=13F-HR&dateb=&owner=include&count=20&output=atom"
    )
    xml_text = get_text(url)
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    results = []
    seen_ciks = set()
    for entry in root.findall("a:entry", ns):
        title_el = entry.find("a:title", ns)
        title = title_el.text.strip() if title_el is not None and title_el.text else "(no name given)"
        link_el = entry.find("a:link", ns)
        href = link_el.get("href") if link_el is not None else ""
        m = re.search(r"CIK=(\d{10})", href)
        if not m:
            continue
        cik = m.group(1).lstrip("0").zfill(10)
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        results.append((cik, title))
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python resolve_cik.py \"Manager Name\"")
        sys.exit(1)
    query = " ".join(sys.argv[1:])
    matches = search_company(query)
    if not matches:
        print(f"No 13F filers found matching '{query}'. Try a shorter or different name fragment.")
        sys.exit(1)
    print(f"Matches for '{query}':\n")
    for cik, name in matches:
        print(f"  CIK {cik}   {name}")
    print("\nConfirm the right entity, then add it to config/managers.json.")
