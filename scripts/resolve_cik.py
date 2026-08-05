"""
Look up a fund manager's CIK directly from EDGAR's own company search — the
authoritative source — instead of trusting a hardcoded number from memory.

Usage:
    python resolve_cik.py "Berkshire Hathaway"
    python resolve_cik.py "Scion Asset Management"

Prints candidate CIKs with their registered name so you can confirm the right
one and paste it into config/managers.json. Run this once per manager when you
set up the pipeline, and again any time a manager's filings look wrong — CIKs
occasionally get reassigned when an entity restructures.
"""
import sys
import xml.etree.ElementTree as ET
from edgar_client import get_text, HEADERS
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
    for entry in root.findall("a:entry", ns):
        title = entry.find("a:title", ns).text
        # title looks like "CIK#0001067983: BERKSHIRE HATHAWAY INC"
        if "CIK#" in title:
            cik_part, name_part = title.split(":", 1)
            cik = cik_part.replace("CIK#", "").strip().lstrip("0").zfill(10)
            results.append((cik, name_part.strip()))
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
