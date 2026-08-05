# TRACE data pipeline

Pulls real data once a day and writes it to `data/*.json`, which the TRACE
frontend fetches directly. GitHub Actions is the "backend" — no server to run.

## Setup (one-time)

1. **Create a GitHub repo**, push this folder plus `trace.html` to it.
2. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `EDGAR_CONTACT_EMAIL` — required. SEC requires a real contact email in
     every request's User-Agent header.
   - `QUIVER_API_KEY` — optional. Raises Congress data from free-tier
     (partial, see below) to full vendor-grade coverage.
   - `TWELVE_DATA_API_KEY` — optional but recommended. Free at
     twelvedata.com (no credit card), 800 requests/day. Without it, every
     "since trade" figure in the app stays "—" instead of a real number.
3. **Verify the 13F manager CIKs** — deliberately manual:
   ```
   pip install -r requirements.txt
   python scripts/resolve_cik.py "Berkshire Hathaway"
   ```
   Confirm the printed entity, then set `"cik"` and `"verified": true` in
   `config/managers.json`. `fetch_13f.py` skips anyone left unverified.
4. **Enable GitHub Pages** (or any static host) so `trace.html` can `fetch()`
   `data/*.json` at a real URL — opening the file from disk blocks `fetch()`,
   and the frontend falls back to demo data in that case.
5. **Run it once manually** (Actions tab → "Daily data refresh" → "Run
   workflow") before trusting the schedule, and read the job logs. See
   "Known untested pieces" below — this first run is how we validate them.

## What's real, what's tiered, what's still a known gap

| Source | Status | Confidence tiers |
|---|---|---|
| Form 4 (insiders) | ✅ Real, free, official EDGAR | Single tier — every record is a directly parsed SEC filing. `filingLagDays` confirms freshness (should be 0-2 days). |
| 13F (hedge funds) | ✅ Real, free, official EDGAR, once CIKs verified | Ticker resolution is `exact` (matched against SEC's own company list) or the issuer's CUSIP is shown honestly instead of a guessed ticker — no fuzzy matching. |
| Congress trades | ⚠️ Tiered, see below | `vendor-api` (Quiver, paid) → `scraped` (Senate eFD, free) → `index-only` (House Clerk, free). Each trade in the output carries its own `confidence` field — the frontend renders them differently rather than implying uniform certainty. |
| Prices / "since trade" | ✅ Real, free (Twelve Data), once `TWELVE_DATA_API_KEY` is set | Historical closes cached forever in `price_cache.json` (a past close never changes); current quotes refreshed daily. Only applied to live-sourced trades — mock/demo rows keep their clearly-synthetic placeholder values, never blended. |

### Congress data tiers, in detail

- **`vendor-api`** — only present if `QUIVER_API_KEY` is set. Full ticker/
  amount/date detail, both chambers, paid ($25-30/mo).
- **`scraped`** — Senate eFD Periodic Transaction Reports, filed as real HTML
  tables since ~2012. Free, structured, Senate only.
- **`index-only`** — House Clerk filings. House PTRs are largely
  scanned/image PDFs with no reliable free text layer, so this tier
  deliberately does NOT attempt OCR-based extraction — that's how wrong
  numbers get into a money app. It instead confirms a filing exists, who
  filed it, and links the actual PDF for manual review.

### Known untested pieces — read this

`scripts/fetch_congress_senate.py` and `scripts/fetch_congress_house.py` were
written without any internet access to test against the live sites. The
overall approach is sound (documented Senate eFD flow; House Clerk's own
bulk-download format), but exact field names, URL patterns, or HTML
structure may have drifted. **Run the workflow once manually and check the
job logs** — both scripts log clear warnings instead of failing silently if
something doesn't match what's expected, so a broken assumption will show up
as a `[warn]` line, not as wrong data in `congress.json`. If you see one,
share the log with me and I'll fix the specific mismatch.

## Rate limits & etiquette

SEC allows ~10 requests/second; this pipeline throttles to ~2/second. The
Senate/House scrapers add small sleeps between requests for the same reason
— none of this needs to run fast, it runs once a day.
