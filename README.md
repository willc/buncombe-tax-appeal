# Buncombe County Property Tax Appeal Tool

A free alternative to paid comp-report services. Pulls comparable sales directly from Buncombe County's public GIS data, scores them by similarity to your property, and generates a self-contained HTML report with:

- **Comparable sales table** — filtered, deduplicated, ranked by similarity score
- **Assessment ratio analysis** — shows assessed ÷ sale price for each comp; flags properties assessed above their sale price
- **Grade challenge math** — if you provide property card details, calculates the dollar impact of a quality grade downgrade (e.g. Grade B → Grade C per the Schedule of Values)
- **Step-by-step appeal guidance** — what to say at the informal appeal and Board of Equalization hearing
- **Board-can-raise warning** — prominently flags the risk before you file
- **Property card checklist** — specific fields to verify from your Spatialest record

> Built for the 2026 Buncombe County reappraisal. Data comes from the county's free ArcGIS API — the same source the COMPER tool uses, but with similarity scoring, ratio analysis, and grade methodology layered on top.

## Install

```bash
git clone https://github.com/willc/buncombe-tax-appeal
cd buncombe-tax-appeal
python3 -m venv venv
venv/bin/pip install requests
```

## Usage

**Basic — just comps:**
```bash
venv/bin/python3 appeal.py "123 Your Street, Asheville"
```

**With property card details (adds grade challenge section):**
```bash
venv/bin/python3 appeal.py "123 Your Street" \
  --grade CUST \
  --sqft 2400 \
  --year-built 2005 \
  --pool-value 0
```

**Run interactively (prompts for address):**
```bash
venv/bin/python3 appeal.py
```

**Widen the date range if comps are sparse:**
```bash
venv/bin/python3 appeal.py "123 Your Street" --since 20200101
```

Output is a self-contained HTML file named `appeal_<address>.html`. Open in any browser, print to PDF to share.

## Options

| Flag | Default | Description |
|---|---|---|
| `address` | (prompted) | Property address |
| `--pin` | — | Look up by PIN instead of address |
| `--since` | `20220101` | Earliest comp sale date (YYYYMMDD) |
| `--grade` | — | Quality grade from property card (CUST, C, B, A, S, L, Q) |
| `--sqft` | — | Heated sq ft from property card |
| `--year-built` | — | Year built |
| `--condition` | `N` | Condition code (R/G/N/F/P/U) |
| `--pool-value` | `0` | Pool assessed value |
| `--yard-items` | `0` | Other yard item assessed values |
| `--output` | auto | Output HTML filename |

Grade analysis section only appears when `--grade`, `--sqft`, and `--year-built` are all provided.

## How it works

**Data source:** Buncombe County GIS FeatureServer (`gis.buncombecounty.org`) — free, no API key required, updated daily.

**Sale price:** Derived from NC Revenue Stamps (`Stamps × $500`). Transfers with stamps below the arm's-length threshold are excluded, per the county's own [Schedule of Values](https://www.buncombenc.gov/588/MyValueBC-2026-Reappraisal) sales qualification rules.

**Comp filtering:**
- Same neighborhood code (broadens to AppraisalArea if fewer than 5 results)
- Building value within ±55% of subject (filters out incomparable properties)
- Class 100, Improved = Y (residential improved only)
- One sale per PIN, most recent (deduplication)

**Similarity score (0–100):**
- Building value proximity: 40 pts
- Acreage proximity: 25 pts
- Sale recency: 25 pts
- Neighborhood match: 10 pts

**Grade analysis** uses quality grade multipliers from the Buncombe County 2026 Schedule of Values (pp. 50, 66) to calculate the dollar impact of reclassifying a property's quality grade. Grade C (Average) = 90–115% of base cost; Grade B (Custom) = 115–140%.

## What the COMPER tool doesn't do

The county's [COMPER tool](https://nc-buncombe-citizen.comper.info/template.aspx) is built on the same assumptions as the assessors' model — same neighborhood boundaries, no scoring, no ratio analysis. This tool goes further:

| | COMPER | This tool |
|---|---|---|
| Comp scoring | None — raw list | Similarity score |
| Assessment ratio | Not shown | Shown for every comp |
| Grade challenge | No | Yes, with $ impact |
| Neighborhood boundary | Hard assessor boundary | Can widen to AppraisalArea |
| Deduplication | No | Yes |
| Appeal guidance | No | Yes, with specific language |

## Limitations

- Square footage, bedrooms, bathrooms, and year built are **not** in the county's public GIS layer. The tool uses building value as a proxy for size/quality. For the most accurate comp matching, verify sq ft from the [property card](https://prc-buncombe.spatialest.com/) and compare manually.
- Sale prices are derived from deed stamps and may not reflect exact transaction amounts (per the county's own disclaimer).
- This is not a licensed appraisal and does not constitute legal advice. Filing a tax appeal is your decision — the Board of Equalization can raise, lower, or confirm your assessed value.

## Resources

- [MyValueBC 2026 Reappraisal](https://www.buncombenc.gov/588/MyValueBC-2026-Reappraisal) — official resources, appeal clinics, Schedule of Values PDF
- [Online Tax Appeal Portal](https://tax.buncombenc.gov/)
- [Property Record Search](https://prc-buncombe.spatialest.com/)
- [Appeal Deadline & Process](https://www.buncombenc.gov/603/Property-Value-Appeals)
