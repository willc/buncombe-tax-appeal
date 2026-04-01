# Buncombe County Property Tax Appeal Tool

A free alternative to paid comp-report services. Pulls comparable sales directly from Buncombe County's public GIS data, scores them by similarity to your property, and generates two reports:

- **Full report** (`appeal_<address>.html`) — internal use; includes appeal strategy, property card checklist, raise-risk warning, and step-by-step online filing guide
- **Upload document** (`appeal_<address>_upload.html`) — clean, professional PDF ready to attach to your online appeal at [tax.buncombenc.gov](https://tax.buncombenc.gov/)

Both reports include:

- **Comparable sales table** — filtered, deduplicated, ranked by similarity score
- **Time-adjusted prices** — sale prices adjusted forward to the Jan 1, 2026 appraisal date using an implied appreciation rate back-calculated from the comps themselves
- **Assessment ratio analysis** — assessed ÷ time-adjusted sale price for every comp; flags over-assessment
- **Requested value** — automatically set to the median time-adjusted comp sale price

> Built for the 2026 Buncombe County reappraisal. Data comes from the county's free ArcGIS API — the same source the COMPER tool uses, but with similarity scoring, ratio analysis, and time weighting layered on top.

## Install

```bash
git clone https://github.com/willc/buncombe-tax-appeal
cd buncombe-tax-appeal
python3 -m venv venv
venv/bin/pip install requests
```

## Update

If you already cloned it, just pull the latest:

```bash
cd buncombe-tax-appeal
git pull
```

## Usage

**Basic — full internal report:**
```bash
venv/bin/python3 appeal.py "123 Your Street, Asheville"
```

**Generate the upload document (attach this to your online appeal):**
```bash
venv/bin/python3 appeal.py "123 Your Street" --upload
```

**With property card details (adds grade challenge section to full report):**
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

**Specify a requested value in the upload document:**
```bash
venv/bin/python3 appeal.py "123 Your Street" --upload --request 650000
```

Output files are self-contained HTML. Open in any browser; use File → Print → Save as PDF to produce the PDF for upload.

## Options

| Flag | Default | Description |
|---|---|---|
| `address` | (prompted) | Property address |
| `--pin` | — | Look up by PIN instead of address |
| `--since` | `20220101` | Earliest comp sale date (YYYYMMDD) |
| `--upload` | off | Generate clean upload document for online appeal portal |
| `--request` | median comp | Requested assessed value to include in upload document |
| `--grade` | — | Quality grade from property card (CUST, C, B, A, S, L, Q) |
| `--sqft` | — | Heated sq ft from property card |
| `--year-built` | — | Year built |
| `--condition` | `N` | Condition code (R/G/N/F/P/U) |
| `--pool-value` | `0` | Pool assessed value |
| `--yard-items` | `0` | Other yard item assessed values |
| `--output` | auto | Output HTML filename |

Grade analysis section only appears in the full report when `--grade`, `--sqft`, and `--year-built` are all provided.

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

**Time weighting:** Older comp sales are adjusted forward to the January 1, 2026 appraisal date. The annual appreciation rate is back-calculated from the comps themselves — by comparing each comp's 2026 assessed value to its actual sale price — so the adjustment reflects what the assessors' own model implies about market trends in your neighborhood. Post-appraisal sales need no adjustment and are labeled accordingly.

**Grade analysis** uses quality grade multipliers from the Buncombe County 2026 Schedule of Values (pp. 50, 66) to calculate the dollar impact of reclassifying a property's quality grade. Grade C (Average) = 90–115% of base cost; Grade B (Custom) = 115–140%.

## What the COMPER tool doesn't do

The county's [COMPER tool](https://nc-buncombe-citizen.comper.info/template.aspx) is built on the same assumptions as the assessors' model — same neighborhood boundaries, no scoring, no ratio analysis. This tool goes further:

| | COMPER | This tool |
|---|---|---|
| Comp scoring | None — raw list | Similarity score (0–100) |
| Assessment ratio | Not shown | Shown for every comp |
| Time-adjusted prices | No | Yes, implied rate from comp data |
| Grade challenge | No | Yes, with $ impact |
| Neighborhood boundary | Hard assessor boundary | Widens to AppraisalArea if needed |
| Deduplication | No | Yes |
| Upload document | No | Yes — PDF-ready for online portal |
| Appeal guidance | No | Yes, with portal filing walkthrough |

## Filing your appeal

1. Run the tool and review the full report to understand your position
2. Run with `--upload` to generate the clean document
3. In your browser, print the upload document → Save as PDF
4. Go to [tax.buncombenc.gov](https://tax.buncombenc.gov/), enter your requested value, and attach the PDF
5. In the "Reason for Appeal" field, keep it brief: *"Assessed value not supported by comparable sales — see attached analysis"*

## Limitations

- Square footage, bedrooms, bathrooms, and year built are **not** in the county's public GIS layer. The tool uses building value as a proxy for size/quality. For the most accurate comp matching, verify sq ft from the [property card](https://prc-buncombe.spatialest.com/) and compare manually.
- Sale prices are derived from deed stamps and may not reflect exact transaction amounts (per the county's own disclaimer).
- This is not a licensed appraisal and does not constitute legal advice. The county can raise, lower, or confirm your assessed value — only file if your comps clearly support a reduction.

## Resources

- [MyValueBC 2026 Reappraisal](https://www.buncombenc.gov/588/MyValueBC-2026-Reappraisal) — official resources, Schedule of Values PDF
- [Online Tax Appeal Portal](https://tax.buncombenc.gov/)
- [Property Record Search](https://prc-buncombe.spatialest.com/)
- [Appeal Deadline & Process](https://www.buncombenc.gov/603/Property-Value-Appeals)
