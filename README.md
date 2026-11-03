# SEC EDGAR Financial Explorer

A web application to fetch and export annual revenue data for **any US-listed company**
using the [SEC EDGAR XBRL API](https://www.sec.gov/developer).

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your SEC identity (required by SEC fair-use policy)
cp .env.example .env
#    Edit .env and fill in SEC_USER_AGENT="Your Name your@email.com"

# 3. Start the web server
uvicorn app:app --reload

# 4. Open http://localhost:8000 in your browser
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web UI |
| `GET /api/fetch?ticker=AMZN&start_year=2020&end_year=2025` | JSON revenue data |
| `GET /api/download?ticker=AMZN&start_year=2020&end_year=2025` | CSV file download |
| `GET /api/sankey?ticker=AMZN&year=2025&period=FY&mode=income` | Sankey diagram (SVG) |
| `GET /api/tickers?q=tesla&limit=8` | Ticker / company-name suggestions (used by the UI dropdowns) |
| `GET /api/health` | Health check |

## CLI (Alternative)

```bash
# Default: AMZN, last 5 years
python fetch_amzn_sec.py

# Any ticker and custom year range
python fetch_amzn_sec.py --ticker MSFT --start-year 2020 --end-year 2025
python fetch_amzn_sec.py --ticker GOOGL --start-year 2018 --end-year 2025 --output data/output/googl.csv
```

## Output Schema

CSV columns exported by `/api/download` and the CLI:

| Column         | Description                                      |
|----------------|--------------------------------------------------|
| company        | Entity name                                      |
| ticker         | AMZN                                             |
| cik            | SEC CIK (zero-padded)                            |
| metric         | XBRL concept tag used                            |
| segment        | `Consolidated`, `North America`, `International`, or `AWS` |
| period_end     | Fiscal period end date (YYYY-MM-DD)              |
| fiscal_year    | Fiscal year integer                              |
| fiscal_period  | `FY`                                             |
| value          | USD value                                        |
| unit           | `USD`                                            |
| form           | SEC form type (`10-K` or `10-K/A`)               |
| accession      | SEC accession number                             |
| filed          | Filing date (YYYY-MM-DD)                         |

## Sankey Diagrams

`/api/sankey` (and the Sankey card in the web UI) draws one fiscal year and period as
a standalone SVG, which the UI can also download as PNG.

| Parameter | Values |
|-----------|--------|
| `ticker`  | Any US-listed ticker |
| `year`    | Fiscal year, as labelled by the company in its filings |
| `period`  | `FY`, `Q1`, `Q2`, `Q3`, `Q4` |
| `mode`    | `income` (default) or `segments` |
| `categories` | `true` (default) or `false`; income mode only |

- **income**: Revenue categories → Revenue → cost of revenue / gross profit → operating expenses / operating
  income → other items → pre-tax income → tax / net income. Gross profit and total
  operating expenses are derived when a company does not report them. Loss-making
  companies stop at the operating stage.
- **Revenue categories** (left column of the income chart) are read from the filing's XBRL
  instance document: product/service lines first, then business segments. Companies tag
  overlapping lines (for example Product, Service and every product line), so the chart
  uses the finest set of non-overlapping lines that adds up to total revenue; more than
  eight are grouped into "Other". If no set adds up, the chart is drawn without them and
  the subtitle says so.
- **segments**: revenue segments → total revenue, read from the filing's XBRL instance
  document (the `companyfacts` API has no segment dimensions). Segment axis is chosen in
  this order: business segments, product/service, geography; only breakdowns that add up
  to total revenue are used.
- **Q4** is never filed separately, so it is derived as the full year minus the Q3
  year-to-date figures.
- Banks, insurers and other issuers without a standard operating income line return a
  404 with an explanation.
- Rendering is pure Python; no headless browser or image libraries are required.

## SEC Access Requirements

The SEC requires all programmatic callers to identify themselves via the
`User-Agent` HTTP header. Failure to do so may result in requests being
blocked. Set `SEC_USER_AGENT` to a string in the format:

```
"Your Name your@email.com"
```

See [SEC EDGAR developer docs](https://www.sec.gov/os/accessing-edgar-data)
for the full fair-use policy. This script enforces a minimum 0.6-second gap
between requests and retries with exponential backoff on 429/503 responses.

## Segment Data Coverage

Amazon's segment breakdown (North America / International / AWS) is reported
in their XBRL filings. This script performs a best-effort scan of all
available `us-gaap` concept tags for segment-specific revenue facts.

**Known limitation:** The SEC `companyfacts` endpoint returns entity-level
(consolidated) facts. Dimensional segment members (XBRL segment axes) are not
surfaced by this endpoint. If structured segment facts are absent from the
API response, the CSV will contain `Consolidated` rows only and the run
summary will note `Segments found: None (v2 scope)`.

v2 scope: parse segment tables directly from the 10-K XBRL instance documents
using the accession numbers present in the output.

## Revenue Concept Selection

The script tries the following XBRL concept tags in priority order and uses
the first one with data:

1. `RevenueFromContractWithCustomerExcludingAssessedTax`
2. `Revenues`
3. `SalesRevenueNet`
4. `RevenueFromContractWithCustomerIncludingAssessedTax`
5. `NetSales`

The concept actually used is printed in the run summary and stored in the
`metric` column.

## Deduplication

When an amended 10-K/A supersedes the original 10-K for a given fiscal year,
only the latest-filed entry is kept.

## Project Structure

```
EdgarData/
├── app.py              # FastAPI web server
├── sec_client.py       # SEC EDGAR XBRL client (any ticker)
├── sankey_data.py      # Sankey models: income statement and segments
├── sankey_render.py    # Pure-Python SVG renderer
├── fetch_amzn_sec.py   # CLI wrapper (any ticker)
├── templates/
│   └── index.html      # Web UI
├── requirements.txt
├── .env.example
├── README.md
└── data/output/        # Generated CSV files
```
