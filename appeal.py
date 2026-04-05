#!/usr/bin/env python3
"""
Buncombe County Property Tax Appeal Tool
Finds comparable sales and generates an appeal packet for the 2026 reappraisal.

Usage:
    python appeal.py                          # uses default property (23 Elkmont Dr)
    python appeal.py "23 Elkmont Drive"       # look up by address
    python appeal.py --pin 973086426200000    # look up by PIN
    python appeal.py --since 20200101         # widen the comp date range

Requires: pip install requests  (or use the venv: venv/bin/python appeal.py)
"""

import requests
import sys
import argparse
from datetime import datetime
from pathlib import Path

ARCGIS_URL = "https://gis.buncombecounty.org/arcgis/rest/services/opendata/FeatureServer/1/query"

# NC deed stamp tax: $2 per $1,000 of consideration → sale price = stamps × 500
# Per the SOV, "Revenue stamps less than $1" = automatic disqualification
MIN_STAMPS = 100          # stamps × $500 = $50,000 minimum sale price
COMP_SALE_START_DATE = "20220101"

# Comp filtering: only include properties with BuildingValue within this ratio of subject
BLDG_VALUE_BAND = 0.55    # ±55% of subject building value

# Buncombe County 2026 reappraisal effective date
APPRAISAL_DATE = datetime(2026, 1, 1)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def arcgis_query(where, out_fields="*", order_by=None, max_records=200):
    params = {
        "where": where,
        "outFields": out_fields,
        "resultRecordCount": max_records,
        "f": "json",
    }
    if order_by:
        params["orderByFields"] = order_by
    r = requests.get(ARCGIS_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return [f["attributes"] for f in data.get("features", [])]


# Municipality codes from Buncombe County GIS (City field = jurisdiction, not mailing city)
CITY_CODES = {
    "CAS": "Asheville",
    "CBF": "Biltmore Forest",
    "CBM": "Black Mountain",
    "CMT": "Montreat",
    "CWO": "Woodfin",
    "CWV": "Weaverville",
}
# ZIP → community name for unincorporated areas
ZIP_CITIES = {
    "28715": "Candler",
    "28730": "Fairview",
    "28748": "Leicester",
    "28778": "Swannanoa",
    "28704": "Arden",
    "28732": "Fletcher",
    "28711": "Black Mountain",
}


def prop_city(attrs):
    """Return the physical city name for a property (not the owner's mailing city)."""
    code = (attrs.get("City") or "").strip()
    if code in CITY_CODES:
        return CITY_CODES[code]
    # Unincorporated — fall back to ZIP-based community name
    zipcode = str(attrs.get("Zipcode") or "").strip()[:5]
    return ZIP_CITIES.get(zipcode, "Asheville")


def prop_address(attrs):
    """Build property address from component fields (not the mailing Address field)."""
    num    = attrs.get("HouseNumber", "").strip()
    pre    = attrs.get("StreetPrefix", "").strip()
    name   = attrs.get("StreetName", "").strip()
    stype  = attrs.get("StreetType", "").strip()
    suf    = (attrs.get("StreetPostDirection") or "").strip()
    parts  = [p for p in [num, pre, name, stype, suf] if p]
    return " ".join(parts)


def lookup_by_address(address):
    parts = address.strip().upper().split()
    num = parts[0] if parts[0].isdigit() else None
    name_part = parts[1] if len(parts) > 1 else parts[0]
    where = f"StreetName LIKE '{name_part}%'"
    if num:
        where += f" AND HouseNumber = '{num}'"
    results = arcgis_query(where)
    if not results:
        return None
    for r in results:
        if r.get("HouseNumber", "") == (num or "") and name_part in r.get("StreetName", ""):
            return r
    return results[0]


def lookup_by_pin(pin):
    results = arcgis_query(f"PIN='{pin}'")
    return results[0] if results else None


def stamps_to_price(stamps):
    if not stamps or stamps < MIN_STAMPS:
        return 0
    return int(stamps * 500)


# ---------------------------------------------------------------------------
# Comp finding, deduplication, and scoring
# ---------------------------------------------------------------------------

def find_comp_candidates(subject):
    """
    Find arm's-length sales near the subject since COMP_SALE_START_DATE.
    Pass 1: same neighborhood (WF-E).
    Pass 2: same AppraisalArea if Pass 1 yields < 5 comps after filtering.
    """
    neighborhood = subject.get("NeighborhoodCode", "")
    appraisal_area = subject.get("AppraisalArea", "")
    subj_bv = int(subject.get("BuildingValue") or 0)
    bv_lo = int(subj_bv * (1 - BLDG_VALUE_BAND))
    bv_hi = int(subj_bv * (1 + BLDG_VALUE_BAND))

    base_where = (
        f" AND DeedDate >= '{COMP_SALE_START_DATE}'"
        f" AND Stamps >= {MIN_STAMPS}"
        f" AND Class = '100'"
        f" AND Improved = 'Y'"
        f" AND CAST(BuildingValue AS INTEGER) > 0"
        f" AND CAST(BuildingValue AS INTEGER) BETWEEN {bv_lo} AND {bv_hi}"
    )

    raw = arcgis_query(
        f"NeighborhoodCode='{neighborhood}'" + base_where,
        order_by="DeedDate DESC"
    )

    if len(raw) < 5 and appraisal_area:
        wider = arcgis_query(
            f"AppraisalArea='{appraisal_area}'" + base_where,
            order_by="DeedDate DESC"
        )
        seen = {r["PIN"] for r in raw}
        for w in wider:
            if w["PIN"] not in seen:
                raw.append(w)

    # Compute sale price and filter
    arm_length = []
    for c in raw:
        price = stamps_to_price(c.get("Stamps"))
        if price > 0:
            c["_sale_price"] = price
            arm_length.append(c)

    # Deduplicate: keep only the most recent sale per PIN
    by_pin = {}
    for c in arm_length:
        pin = c.get("PIN")
        if pin not in by_pin or c.get("DeedDate", "") > by_pin[pin].get("DeedDate", ""):
            by_pin[pin] = c
    return list(by_pin.values())


def score_comp(subject, comp):
    """
    Similarity 0–100. Weights:
      Building value proximity  40 pts
      Acreage proximity         25 pts
      Sale recency              25 pts
      Neighborhood match        10 pts
    """
    score = 100.0

    # Building value (40 pts)
    subj_bv = int(subject.get("BuildingValue") or 0)
    comp_bv = int(comp.get("BuildingValue") or 0)
    if subj_bv > 0 and comp_bv > 0:
        diff = abs(subj_bv - comp_bv) / subj_bv
        score -= min(40, diff * 100)

    # Acreage (25 pts)
    subj_ac = float(subject.get("Acreage") or 0)
    comp_ac = float(comp.get("Acreage") or 0)
    if subj_ac > 0 and comp_ac > 0:
        diff = abs(subj_ac - comp_ac) / subj_ac
        score -= min(25, diff * 60)

    # Recency (25 pts) — lose 7 pts per year of age
    try:
        sale_year = int(str(comp.get("DeedDate", "20220101"))[:4])
        years_ago = datetime.now().year - sale_year
        score -= min(25, years_ago * 7)
    except (ValueError, TypeError):
        score -= 12

    # Neighborhood (10 pts)
    if comp.get("NeighborhoodCode") != subject.get("NeighborhoodCode"):
        score -= 10

    return max(0.0, round(score, 1))


def assessment_ratio(assessed, sale_price):
    if not sale_price or sale_price <= 0:
        return None
    return assessed / sale_price


# ---------------------------------------------------------------------------
# Time weighting
# ---------------------------------------------------------------------------

def years_before_appraisal(deed_date_str):
    """Years between sale date and Jan 1 2026. Returns 0 for post-appraisal sales."""
    try:
        s = str(deed_date_str)
        sale_dt = datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))
        delta = (APPRAISAL_DATE - sale_dt).days / 365.25
        return max(0.0, delta)
    except Exception:
        return 0.0


def implied_annual_rate(comps):
    """
    Back-calculate the annual appreciation rate baked into the assessors' model by
    comparing each comp's 2026 assessed value to its actual sale price.
    rate = (assessed / sale_price) ^ (1 / years) - 1
    Returns the median rate across all pre-appraisal comps.
    """
    rates = []
    for c in comps:
        sale_price = c.get("_sale_price", 0)
        assessed   = int(c.get("TotalMarketValue") or 0)
        years      = years_before_appraisal(c.get("DeedDate"))
        if sale_price > 0 and assessed > 0 and years >= 0.5:
            r = (assessed / sale_price) ** (1.0 / years) - 1.0
            if -0.10 < r < 0.30:   # sanity-check: −10% to +30%/yr
                rates.append(r)
    if not rates:
        return 0.04   # fallback: 4 %/yr if insufficient data
    return sorted(rates)[len(rates) // 2]


def time_adjusted_price(sale_price, deed_date_str, annual_rate):
    """Adjust sale price forward to Jan 1 2026 at the given annual rate."""
    years = years_before_appraisal(deed_date_str)
    if years == 0:
        return sale_price   # post-appraisal sale — no adjustment
    return int(sale_price * (1 + annual_rate) ** years)


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def fmt_money(val):
    if val is None:
        return "—"
    return f"${val:,.0f}"


def fmt_ratio(r):
    if r is None:
        return "—"
    pct = r * 100
    if r > 1.05:
        style = "color:#c0392b;font-weight:600"
        label = f"{pct:.1f}% ▲"
    elif r < 0.95:
        style = "color:#27ae60;font-weight:600"
        label = f"{pct:.1f}% ★"
    else:
        style = "color:#555"
        label = f"{pct:.1f}%"
    return f'<span style="{style}" title="Assessed ÷ Sale Price">{label}</span>'


def fmt_date(d):
    try:
        s = str(d)
        return f"{s[4:6]}/{s[6:8]}/{s[:4]}"
    except Exception:
        return str(d)


# ---------------------------------------------------------------------------
# Grade impact analysis
# ---------------------------------------------------------------------------

# SOV Table: Grade midpoint multipliers (Schedule of Values pp. 50, 66)
GRADE_MULTS = {
    "Q": 5.00, "L": 2.575, "S": 2.05, "A": 1.70,
    "B": 1.275, "CUST": 1.275,
    "C": 1.025,
    "D": 0.85, "E": 0.625,
}
# Depreciation % for Normal condition by schedule / age (approximate from charts)
# For a 7-year-old home (2019) in Normal condition, R-80/R-85 schedule → ~6% depreciation
# We reverse-engineer base rate from building value and grade multiplier.

def grade_analysis(building_value, grade_code, sq_ft, year_built,
                   pool_value=0, yard_items=0, land_value=0):
    """
    Returns a dict with grade impact calculations for the HTML report.
    """
    current_year = datetime.now().year
    age = current_year - year_built

    current_mult = GRADE_MULTS.get(grade_code.upper(), 1.275)
    c_mult       = GRADE_MULTS["C"]   # Grade C midpoint

    # Reverse-engineer: base_rate_sqft = RCN / (grade_mult × sq_ft)
    # We estimate ~6% depreciation for a 7-year Normal-condition home (SOV R-80 chart)
    # Depreciation estimate: 0.9% per year for Normal condition on newer homes (approximate)
    depr_pct = min(age * 0.009, 0.25)   # cap at 25% for this estimate
    pct_good  = 1 - depr_pct
    rcn_current = building_value / pct_good   # replacement cost new at current grade

    base_cost_total = rcn_current / current_mult   # base cost (Grade C = 100%)
    base_rate_sqft  = base_cost_total / sq_ft if sq_ft else 0

    # Value at Grade C midpoint
    rcn_c   = base_cost_total * c_mult
    bldg_c  = rcn_c * pct_good
    total_c = bldg_c + pool_value + yard_items + land_value

    # Value at Grade B low end (115%)
    rcn_b_lo   = base_cost_total * 1.15
    bldg_b_lo  = rcn_b_lo * pct_good
    total_b_lo = bldg_b_lo + pool_value + yard_items + land_value

    # Value at Grade B midpoint (current)
    total_current = building_value + pool_value + yard_items + land_value

    return {
        "grade_code":      grade_code.upper(),
        "current_mult":    current_mult,
        "age":             age,
        "depr_pct":        depr_pct,
        "base_rate_sqft":  base_rate_sqft,
        "bldg_current":    building_value,
        "total_current":   total_current,
        "bldg_c":          int(bldg_c),
        "total_c":         int(total_c),
        "bldg_b_lo":       int(bldg_b_lo),
        "total_b_lo":      int(total_b_lo),
        "savings_c":       int(total_current - total_c),
        "savings_b_lo":    int(total_current - total_b_lo),
    }


# ---------------------------------------------------------------------------
# Grade section HTML builder
# ---------------------------------------------------------------------------

def grade_section_html(ga, assessed, prop_card):
    if not ga:
        return ""
    rows = f"""
    <tr>
      <td>Current: Grade <strong>{ga['grade_code']}</strong> (Custom/above-avg)</td>
      <td style="text-align:right">{fmt_money(ga['bldg_current'])}</td>
      <td style="text-align:right"><strong style="color:#c0392b">{fmt_money(assessed)}</strong></td>
      <td style="text-align:right">—</td>
    </tr>
    <tr style="background:#fff8f0">
      <td>Scenario A: Grade <strong>B low end</strong> (115% × base)</td>
      <td style="text-align:right">{fmt_money(ga['bldg_b_lo'])}</td>
      <td style="text-align:right"><strong>{fmt_money(ga['total_b_lo'])}</strong></td>
      <td style="text-align:right;color:#27ae60">−{fmt_money(ga['savings_b_lo'])}</td>
    </tr>
    <tr style="background:#f0fff4">
      <td>Scenario B: Grade <strong>C</strong> (Average, 102.5% × base)</td>
      <td style="text-align:right">{fmt_money(ga['bldg_c'])}</td>
      <td style="text-align:right"><strong>{fmt_money(ga['total_c'])}</strong></td>
      <td style="text-align:right;color:#27ae60">−{fmt_money(ga['savings_c'])}</td>
    </tr>
    """
    base_rate = ga['base_rate_sqft']
    depr = ga['depr_pct'] * 100
    return f"""
<div class="alert" style="border-color:#9b59b6;background:#f9f0ff">
  <h3 style="color:#7d3c98">Grade B (CUST) adds a significant premium — here's the math</h3>
  <p style="margin-bottom:12px">
    The assessors classified your home as <strong>Grade B "Custom"</strong>, which multiplies the base
    construction cost by <strong>115–140%</strong> (vs. Grade C Average at 90–115%).
    At the Grade B midpoint (127.5×), your implied base construction rate is
    <strong>{fmt_money(int(base_rate))}/sqft</strong>
    with approximately <strong>{depr:.1f}% depreciation</strong> applied
    (age {ga['age']} years, Normal condition).
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:0.92em;margin-bottom:12px">
    <thead>
      <tr style="background:#7d3c98;color:white">
        <th style="padding:8px 12px;text-align:left">Grade Scenario</th>
        <th style="padding:8px 12px;text-align:right">Bldg Value</th>
        <th style="padding:8px 12px;text-align:right">Total Assessed</th>
        <th style="padding:8px 12px;text-align:right">Savings</th>
      </tr>
    </thead>
    <tbody style="background:white">
      {rows}
    </tbody>
  </table>
  <p style="font-size:0.9em"><strong>How to argue this at the hearing:</strong>
  The Schedule of Values (p. 56–57) defines Grade B as requiring
  <em>"attention to architectural design in refinements and details,"</em>
  <em>"good quality standard materials,"</em> and construction that <em>"generally exceeds minimum
  building codes."</em> Grade C (p. 57–58) is <em>"basic design and features, average quality materials
  and workmanship with moderate architectural styling."</em><br><br>
  If your home was built from a builder's stock plan (not a custom architect-designed plan), uses
  standard builder-grade materials (stock cabinetry, standard fixtures, composition shingles),
  and does not have custom ornamentation or special features throughout, it qualifies as Grade C.
  Grade B requires clearly above-average materials and workmanship — not just "nice for the price."<br><br>
  Ask the assessor: <em>"What specific features of this home justify Grade B over Grade C?
  Can you show me the field notes from the inspection?"</em>
  Then compare their answer to the SOV descriptions.
  Even a reclassification to B-low (115%) saves ~{fmt_money(ga['savings_b_lo'])}.
  </p>
</div>"""


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def generate_html(subject, comps_scored, output_path, card=None, rate=0.0):
    subj_assessed = int(subject.get("TotalMarketValue") or 0)
    subj_bldg    = int(subject.get("BuildingValue") or 0)
    subj_land    = int(subject.get("LandValue") or 0)
    subj_ac      = float(subject.get("Acreage") or 0)
    neighborhood = subject.get("NeighborhoodCode", "")
    prop_card    = subject.get("PropCard", "")
    subj_addr    = (f"{subject.get('HouseNumber','')} {subject.get('StreetName','')} "
                   f"{subject.get('StreetType','')}, {prop_city(subject)}, NC "
                   f"{subject.get('Zipcode','')}").strip()

    # Property card data (optional)
    ga = None
    if card:
        ga = grade_analysis(
            building_value = subj_bldg,
            grade_code     = card.get("grade", "B"),
            sq_ft          = card.get("sq_ft", 0),
            year_built     = card.get("year_built", 2000),
            pool_value     = card.get("pool_value", 0),
            yard_items     = card.get("yard_items", 0),
            land_value     = subj_land,
        )

    # Statistics — use time-adjusted prices as primary
    adj_prices    = [c["_adj_price"] for c in comps_scored]
    adj_ratios    = [c["_adj_ratio"] for c in comps_scored if c["_adj_ratio"] is not None]

    median_ratio  = sorted(adj_ratios)[len(adj_ratios) // 2] if adj_ratios else None
    avg_ratio     = sum(adj_ratios) / len(adj_ratios) if adj_ratios else None
    median_price  = sorted(adj_prices)[len(adj_prices) // 2] if adj_prices else None
    above_assessed = [c for c in comps_scored if c["_adj_price"] < subj_assessed]
    high_ratio_comps = [c for c in comps_scored if c["_adj_ratio"] and c["_adj_ratio"] > 1.05]

    # ---- Comp rows ----
    rows = ""
    for i, c in enumerate(comps_scored):
        ratio = c["_ratio"]
        flag = ""
        adj_ratio = c.get("_adj_ratio")
        is_post_appraisal = c.get("_years", 1) == 0
        adj_label = "post-appraisal" if is_post_appraisal else fmt_money(c["_adj_price"])
        if adj_ratio and adj_ratio < 0.95:
            flag = '<span title="Time-adjusted price below assessed value — strong comp" style="color:#27ae60;margin-left:4px">★</span>'
        elif adj_ratio and adj_ratio > 1.10:
            flag = '<span title="Assessed above time-adjusted price — supports your appeal" style="color:#c0392b;margin-left:4px">▲</span>'
        else:
            flag = ""
        rows += f"""
        <tr>
          <td style="text-align:center;color:#888">{i+1}</td>
          <td><strong>{prop_address(c)}</strong><br>
              <span style="font-size:0.8em;color:#999">{prop_city(c)} · {c.get('NeighborhoodCode','')}</span></td>
          <td>{fmt_date(c.get('DeedDate'))}</td>
          <td style="text-align:right">{fmt_money(c['_sale_price'])}</td>
          <td style="text-align:right"><strong>{adj_label}</strong></td>
          <td style="text-align:right">{fmt_money(int(c.get('TotalMarketValue') or 0))}</td>
          <td style="text-align:center">{fmt_ratio(adj_ratio)}{flag}</td>
          <td style="text-align:right">{fmt_money(int(c.get('BuildingValue') or 0))}</td>
          <td style="text-align:right">{float(c.get('Acreage') or 0):.2f} ac</td>
          <td style="text-align:center;color:#888">{c.get('_score','')}</td>
          <td style="text-align:center"><a href="https://prc-buncombe.spatialest.com/#/property/{c.get('PIN','')}" target="_blank" style="font-size:0.8em;color:#3498db">Card</a></td>
        </tr>"""

    # ---- Appeal analysis bullets ----
    bullets = []
    if median_price and median_price < subj_assessed:
        over_pct = (subj_assessed - median_price) / subj_assessed * 100
        bullets.append(
            f"The <strong>median time-adjusted sale price</strong> of the {len(comps_scored)} filtered "
            f"comparable properties is <strong>{fmt_money(median_price)}</strong> — "
            f"your 2026 assessed value of {fmt_money(subj_assessed)} is "
            f"<strong>{over_pct:.1f}% above</strong> the median comp sale."
        )
    if len(above_assessed) > 0:
        bullets.append(
            f"<strong>{len(above_assessed)} of {len(comps_scored)} comparable properties</strong> "
            f"sold for less than your assessed value of {fmt_money(subj_assessed)}, "
            f"indicating the market does not support this assessment level."
        )
    if median_ratio:
        bullets.append(
            f"The <strong>median assessment ratio</strong> (assessed ÷ sale price) for comps is "
            f"<strong>{median_ratio*100:.1f}%</strong>. "
            + ("The county's own SOV states the target ratio is 95–105%. "
               "A ratio significantly above 100% on your comps indicates systematic over-assessment in this neighborhood."
               if median_ratio > 1.05 else
               "Properties similar to yours are selling at or below their assessed values.")
        )
    if not bullets:
        bullets.append("Review the comp table below. Select the strongest comps (★ or ▲ marked) and use their sale prices as evidence that your property's market value is lower than assessed.")

    bullets_html = "\n".join(f"<li>{b}</li>" for b in bullets)

    # ---- What to check on your property card ----
    checklist = """
    <li><strong>Building Quality Grade</strong> — Should be C (average), B (custom), A (superior), etc.
        Check it against the Grade Descriptions in the Schedule of Values. Even one grade bump (C→B)
        inflates value by 15–40%. If your home has stock materials and no custom features, it should not exceed Grade C.</li>
    <li><strong>Condition Code</strong> — R (Renovated), G (Good), N (Normal), F (Fair), P (Poor).
        If your home has deferred maintenance, aging systems, or was never renovated, "Normal" may be too generous.
        A downgrade from N to F significantly increases depreciation and lowers assessed value.</li>
    <li><strong>Year Built / Effective Age</strong> — Older homes receive more depreciation. If an incorrect
        year built is recorded, depreciation may be understated. Also check if major systems (roof, HVAC, plumbing)
        are original — older systems can support a higher effective age than the calendar age.</li>
    <li><strong>Square Footage</strong> — The base cost × sq ft × quality grade = replacement cost new.
        Any error here flows directly into the final value. Measure your heated/finished area and compare
        to what the property card shows. Finished vs. unfinished basement, porch, etc. can all affect this.</li>
    <li><strong>Building Style Code</strong> — Ranch (RAN), 1-Story Conventional (1CN), Contemporary (CON), etc.
        Each style has a different base cost per sq ft. An incorrect style code (e.g., coded as 2CN when
        it's a ranch) will produce a wrong base cost.</li>
    <li><strong>Special Features / Yard Items</strong> — Check that no features are listed that don't exist
        (e.g., a pool, deck, or outbuilding you don't have). These add directly to assessed value.</li>
    <li><strong>Neighborhood Code</strong> — Your property is in <strong>{neighborhood}</strong>. If a neighboring
        property with the same characteristics is in a lower-value neighborhood code and was assessed lower,
        this is evidence of inequitable assessment. Neighborhood boundaries are set by the assessor and
        can be challenged.</li>
    """.format(neighborhood=neighborhood)

    # ---- Step-by-step appeal guidance ----
    appeal_steps = f"""
    <ol class="steps">
      <li>
        <strong>Get your property card first.</strong>
        Open <a href="{prop_card}" target="_blank">your property record card</a> and note your building
        quality grade, condition code, year built, sq ft, and style code.
        These are the inputs to the cost model — any error here is grounds for appeal.
      </li>
      <li>
        <strong>Pick your 3–5 best comps from the table below.</strong>
        The strongest comps are properties that (a) sold recently, (b) are similar in size and quality,
        and (c) sold for less than your assessed value (marked ★). Check their property cards to
        confirm they have similar sq ft, grade, and features to yours. Note the PIN for each one.
      </li>
      <li>
        <strong>Calculate your requested value.</strong>
        For each strong comp: <em>your assessed value × (comp sale price ÷ comp assessed value)</em>.
        This is the value your property <em>would</em> be if assessed at the same ratio as the comp.
        Average these to get your requested value. Example: if a comp sold for $700K but was assessed
        at $607K (86.7% ratio) and your property is assessed at {fmt_money(subj_assessed)},
        the implied value = {fmt_money(subj_assessed)} × 86.7% = {fmt_money(int(subj_assessed * 0.867))}.
      </li>
      <li>
        <strong>Generate your upload document.</strong>
        Run this tool with the <code>--upload</code> flag to produce a clean, professional PDF-ready
        document you can attach to your online appeal. It contains your comp table, summary stats,
        and requested value — with no internal notes.
      </li>
      <li>
        <strong>File online at the Buncombe County Tax Appeal Portal.</strong>
        Go to <a href="https://tax.buncombenc.gov/" target="_blank">tax.buncombenc.gov</a>.
        Enter your requested value, briefly describe your reason (e.g., "Assessed value not supported
        by comparable sales — see attached analysis"), and upload your document.
        The portal accepts PDF; print your upload document to PDF from your browser first.
      </li>
      <li>
        <strong>In the "Reason for Appeal" field</strong>, keep it short and factual:
        <em>"The 2026 assessed value of {fmt_money(subj_assessed)} is not supported by recent
        arm's-length sales of comparable properties in neighborhood {neighborhood}.
        Comparable sales since 2022 indicate a market value of approximately $[your number].
        Supporting comp analysis attached."</em>
      </li>
    </ol>
    """

    # ---- SOV methodology notes ----
    sov_notes = """
    <p>The 2026 reappraisal used <strong>AppraisalEst</strong> (a GIS-based MRA + comparable sales model).
    The model weighted comps by <em>distance × attribute similarity</em> (sq ft, quality grade, age, style).
    It then applied a <strong>similarity-weighted mean</strong> of adjusted comparable sale prices.
    Key vulnerabilities in this model:</p>
    <ul>
      <li><strong>Neighborhood boundary effect</strong>: The model is constrained to the assigned
      neighborhood code. If your street sits at the edge of a higher-value neighborhood boundary,
      your comps skew high. Request the assessor show which sales were used for your property.</li>
      <li><strong>Quality grade subjectivity</strong>: The SOV states "appraisers may use their judgment"
      for grade adjustments. Grade C (average) multiplies base cost by 0.9–1.15×; Grade B by 1.15–1.4×.
      An assessor who upgraded your home from C to B+ inflated the building value by 15–25%.</li>
      <li><strong>Depreciation schedule assignment</strong>: Economic life is assigned by building type
      (R-45, R-80, R-85, etc.). A newer or well-maintained home gets a longer economic life and
      therefore less depreciation. If your home's effective age is understated, depreciation is too low.</li>
      <li><strong>Machine-learning base rate</strong>: The SAS regression-tree also set a base $/sqft rate
      based on sales leading up to the appraisal date. If your area had a spike in luxury sales,
      the base rate for the entire neighborhood may be inflated.</li>
    </ul>
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Tax Appeal Report — {subj_addr}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         font-size: 14px; line-height: 1.6; color: #222; background: #f5f6f8; }}
  .page {{ max-width: 1160px; margin: 0 auto; padding: 36px 24px; }}
  h1 {{ font-size: 1.7em; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1.05em; font-weight: 700; margin: 28px 0 10px;
       border-bottom: 2px solid #e0e3e8; padding-bottom: 6px; text-transform: uppercase;
       letter-spacing: 0.04em; color: #2c3e50; }}
  h3 {{ font-size: 0.95em; font-weight: 700; color: #444; margin-bottom: 6px; }}
  .meta {{ color: #777; font-size: 0.87em; margin-bottom: 28px; }}
  .meta a {{ color: #3498db; }}

  /* Stat cards */
  .cards {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 24px; }}
  .card {{ background: white; border-radius: 10px; padding: 16px 20px; min-width: 140px;
           flex: 1; box-shadow: 0 1px 4px rgba(0,0,0,.07); }}
  .card .lbl {{ font-size: 0.73em; text-transform: uppercase; letter-spacing: 0.06em;
               color: #999; margin-bottom: 3px; }}
  .card .val {{ font-size: 1.45em; font-weight: 700; line-height: 1.2; }}
  .card .sub {{ font-size: 0.78em; color: #aaa; margin-top: 2px; }}
  .card.red .val {{ color: #c0392b; }}
  .card.green .val {{ color: #27ae60; }}

  /* Alert box */
  .alert {{ border-left: 4px solid #e67e22; background: #fff8f0; padding: 16px 20px;
            border-radius: 6px; margin-bottom: 20px; }}
  .alert h3 {{ color: #d35400; margin-bottom: 8px; }}
  .alert ul {{ margin-left: 20px; }}
  .alert ul li {{ margin-bottom: 6px; }}

  /* Info box */
  .info {{ border-left: 4px solid #3498db; background: #f0f7ff; padding: 16px 20px;
           border-radius: 6px; margin-bottom: 20px; }}
  .info ul {{ margin-left: 20px; }}
  .info ul li {{ margin-bottom: 7px; }}
  .info p {{ margin-bottom: 8px; }}

  /* Steps */
  .steps {{ margin-left: 20px; }}
  .steps li {{ margin-bottom: 12px; }}

  /* Warning box */
  .warn {{ border-left: 4px solid #9b59b6; background: #f9f0ff; padding: 16px 20px;
           border-radius: 6px; margin-bottom: 20px; font-size: 0.9em; }}
  .warn h3 {{ color: #7d3c98; margin-bottom: 8px; }}
  .warn ul {{ margin-left: 20px; }}
  .warn ul li {{ margin-bottom: 6px; }}

  /* Table */
  .tbl-wrap {{ overflow-x: auto; margin-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px;
           overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.07); font-size: 0.9em; }}
  th {{ background: #2c3e50; color: #e8ecf0; padding: 9px 12px; text-align: left;
        font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #f0f2f5; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #fafbfc; }}

  .note {{ font-size: 0.8em; color: #999; margin-top: 10px; line-height: 1.6; }}
  .note strong {{ color: #666; }}

  @media print {{
    body {{ background: white; font-size: 12px; }}
    .page {{ padding: 12px; max-width: none; }}
    .card, table {{ box-shadow: none; border: 1px solid #ddd; }}
    .alert, .info, .warn {{ border: 1px solid #ddd; }}
    a {{ color: inherit; text-decoration: none; }}
  }}
</style>
</head>
<body>
<div class="page">

<h1>Property Tax Appeal Report</h1>
<p class="meta">
  Generated {datetime.now().strftime('%B %d, %Y')} &nbsp;·&nbsp;
  <strong>{subj_addr}</strong> &nbsp;·&nbsp;
  PIN: {subject.get('PIN')} &nbsp;·&nbsp;
  Neighborhood: <strong>{neighborhood}</strong> &nbsp;·&nbsp;
  AppraisalArea: {subject.get('AppraisalArea','')} &nbsp;·&nbsp;
  <a href="{prop_card}" target="_blank">View Property Card ↗</a>
</p>

<!-- ======================================================= -->
<h2>Subject Property — 2026 Assessment</h2>
<div class="cards">
  <div class="card red">
    <div class="lbl">2026 Assessed Value</div>
    <div class="val">{fmt_money(subj_assessed)}</div>
  </div>
  <div class="card">
    <div class="lbl">Building Value</div>
    <div class="val">{fmt_money(subj_bldg)}</div>
    <div class="sub">{subj_bldg/subj_assessed*100:.0f}% of total</div>
  </div>
  <div class="card">
    <div class="lbl">Land Value</div>
    <div class="val">{fmt_money(subj_land)}</div>
    <div class="sub">{subj_land/subj_assessed*100:.0f}% of total</div>
  </div>
  <div class="card">
    <div class="lbl">Lot Size</div>
    <div class="val">{subj_ac:.3f} ac</div>
  </div>
  <div class="card">
    <div class="lbl">Comparable Sales</div>
    <div class="val">{len(comps_scored)}</div>
    <div class="sub">filtered, since {COMP_SALE_START_DATE[:4]}</div>
  </div>
  {'<div class="card ' + ('red' if median_ratio and median_ratio > 1.05 else 'green') + '"><div class="lbl">Median Comp Ratio</div><div class="val">' + (f"{median_ratio*100:.1f}%" if median_ratio else "—") + '</div><div class="sub">assessed ÷ sale price</div></div>' if median_ratio else ''}
  {'<div class="card ' + ('red' if median_price and median_price < subj_assessed else 'green') + '"><div class="lbl">Median Comp Sale</div><div class="val">' + fmt_money(median_price) + '</div><div class="sub">vs your ' + fmt_money(subj_assessed) + ' assessment</div></div>' if median_price else ''}
</div>

<!-- ======================================================= -->
<h2>Appeal Analysis</h2>
<div class="alert">
  <h3>Evidence Supporting a Reduced Assessment</h3>
  <ul>
    {bullets_html}
  </ul>
</div>

<!-- ======================================================= -->
<div style="background:#fff0f0;border-left:4px solid #c0392b;border-radius:6px;padding:14px 20px;margin-bottom:20px">
  <strong style="color:#c0392b">⚠ Important before you file:</strong>
  The county has authority to <strong>raise, lower, or confirm</strong> your assessed value — not just
  lower it. If the evidence shows your property is <em>under</em>-assessed relative to comparable sales,
  filing could result in an increase.
  Review your comps carefully: if most comparable properties sold <em>above</em> your current
  assessed value, filing an appeal carries risk. Only file if the evidence clearly supports a reduction.
</div>

<!-- ======================================================= -->
<h2>Step-by-Step Appeal Guide</h2>
<div class="info">
  {appeal_steps}
</div>

<!-- ======================================================= -->
<h2>What to Verify on Your Property Card</h2>
<div class="warn">
  <h3>Check These Fields at <a href="{prop_card}" target="_blank">{prop_card}</a></h3>
  <ul>
    {checklist}
  </ul>
</div>

<!-- ======================================================= -->
<h2>Comparable Sales — Neighborhood {neighborhood}
    <span style="font-weight:400;font-size:0.85em;color:#888">
      (BuildingValue ±{int(BLDG_VALUE_BAND*100)}% of subject · arm's-length sales since {COMP_SALE_START_DATE[:4]} · deduplicated by PIN)
    </span>
</h2>
<div class="tbl-wrap">
<table>
  <thead>
    <tr>
      <th>#</th>
      <th>Address</th>
      <th>Sale Date</th>
      <th>Raw Sale Price</th>
      <th title="Sale price adjusted forward to Jan 1, 2026 at implied {rate*100:.1f}%/yr market trend">Adj. to Jan 2026</th>
      <th>2026 Assessed</th>
      <th>Ratio (Assessed÷Adj.)</th>
      <th>Bldg Value</th>
      <th>Lot</th>
      <th>Score</th>
      <th>Card</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>
</div>

<p class="note">
  <strong>★ Green</strong> = time-adjusted price below assessed value — supports lower assessment.
  <strong>▲ Red</strong> = assessed above time-adjusted price.<br>
  <strong>Adj. to Jan 2026</strong> applies an implied <strong>{rate*100:.1f}%/yr</strong> market appreciation rate
  (back-calculated from the median relationship between comp assessed values and their actual sale prices).
  Post-appraisal sales are marked "post-appraisal" and need no adjustment.
  The ratio column uses the time-adjusted price — this is what the assessors' model effectively used.<br>
  <strong>Sale Price</strong> derived from NC Revenue Stamps (Stamps × $500). Transfers with stamps &lt; {MIN_STAMPS} excluded (per SOV sales qualification rules).<br>
  <strong>Score</strong> = similarity to your property (building value 40%, acreage 25%, recency 25%, neighborhood 10%).
  Click "Card" to open any comp's full property record.
</p>

<!-- ======================================================= -->
{'<h2>Property Card Analysis — Grade Challenge</h2>' + grade_section_html(ga, subj_assessed, prop_card) if ga else ''}

<!-- ======================================================= -->
<h2>How the Assessors Valued Your Property (Schedule of Values Summary)</h2>
<div class="info">
  {sov_notes}
</div>

</div>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Clean submission document (no internal guidance, safe to send to board)
# ---------------------------------------------------------------------------

def generate_submission_html(subject, comps_scored, output_path, card=None, requested_value=None, rate=0.0):
    subj_assessed = int(subject.get("TotalMarketValue") or 0)
    subj_bldg    = int(subject.get("BuildingValue") or 0)
    subj_land    = int(subject.get("LandValue") or 0)
    subj_ac      = float(subject.get("Acreage") or 0)
    neighborhood = subject.get("NeighborhoodCode", "")
    subj_pin     = subject.get("PIN", "")
    subj_addr    = (f"{subject.get('HouseNumber','')} {subject.get('StreetName','')} "
                   f"{subject.get('StreetType','')}, {prop_city(subject)}, NC "
                   f"{subject.get('Zipcode','')}").strip()

    today = datetime.now().strftime("%B %d, %Y")

    adj_prices = [c["_adj_price"] for c in comps_scored]
    adj_ratios = [c["_adj_ratio"] for c in comps_scored if c["_adj_ratio"]]
    median_price = sorted(adj_prices)[len(adj_prices)//2] if adj_prices else None
    median_ratio = sorted(adj_ratios)[len(adj_ratios)//2] if adj_ratios else None

    ga = None  # grade challenge excluded from submission document

    # Comp rows — clean, no score column
    rows = ""
    for i, c in enumerate(comps_scored):
        adj_ratio = c["_adj_ratio"]
        ratio_str = f"{adj_ratio*100:.1f}%" if adj_ratio else "—"
        is_post_appraisal = c.get("_years", 1) == 0
        adj_label = "post-appraisal" if is_post_appraisal else fmt_money(c["_adj_price"])
        rows += f"""
        <tr>
          <td>{i+1}</td>
          <td><strong>{prop_address(c)}</strong>, {prop_city(c)}, NC</td>
          <td>{c.get('PIN','')}</td>
          <td>{fmt_date(c.get('DeedDate'))}</td>
          <td style="text-align:right">{fmt_money(c['_sale_price'])}</td>
          <td style="text-align:right"><strong>{adj_label}</strong></td>
          <td style="text-align:right">{fmt_money(int(c.get('TotalMarketValue') or 0))}</td>
          <td style="text-align:right">{ratio_str}</td>
          <td style="text-align:right">{fmt_money(int(c.get('BuildingValue') or 0))}</td>
          <td style="text-align:right">{float(c.get('Acreage') or 0):.2f} ac</td>
        </tr>"""

    # Grade section
    grade_html = ""
    if ga:
        grade_html = f"""
        <div class="section">
          <h2>III. Quality Grade Analysis</h2>
          <p>The subject property is classified as Quality Grade <strong>{ga['grade_code']} (Custom/Above-Average)</strong>
          per the 2026 Buncombe County Schedule of Values. The Schedule of Values (pp. 56–57) defines
          Grade B as requiring custom architectural design, above-average materials, and workmanship
          that clearly exceeds minimum building code standards.</p>
          <p style="margin-top:10px">The subject was constructed from a builder's stock plan using standard
          builder-grade materials and finishes. Per the Schedule of Values definitions, Quality Grade C
          (Average) — representing "basic design and features, average quality materials and workmanship
          with moderate architectural styling" — is the appropriate classification.</p>
          <table style="margin-top:14px">
            <thead>
              <tr>
                <th>Grade Scenario</th>
                <th style="text-align:right">Building Value</th>
                <th style="text-align:right">Total Assessed Value</th>
                <th style="text-align:right">Difference</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Current: Grade {ga['grade_code']} (Custom, 115–140% of base)</td>
                <td style="text-align:right">{fmt_money(ga['bldg_current'])}</td>
                <td style="text-align:right"><strong>{fmt_money(ga['total_current'])}</strong></td>
                <td style="text-align:right">—</td>
              </tr>
              <tr style="background:#f9f9f9">
                <td>Grade C (Average, 90–115% of base) — requested</td>
                <td style="text-align:right">{fmt_money(ga['bldg_c'])}</td>
                <td style="text-align:right"><strong>{fmt_money(ga['total_c'])}</strong></td>
                <td style="text-align:right">−{fmt_money(ga['savings_c'])}</td>
              </tr>
            </tbody>
          </table>
        </div>"""

    # Requested value block
    req_val = requested_value or (ga['total_c'] if ga else median_price)
    req_html = ""
    if req_val:
        req_html = f"""
        <div class="section req">
          <h2>Requested Value</h2>
          <p>Based on the comparable sales evidence above, I respectfully request a reduction in the
          assessed value of <strong>{subj_addr}</strong> (PIN {subj_pin})
          from <strong>{fmt_money(subj_assessed)}</strong>
          to <strong>{fmt_money(int(req_val))}</strong>,
          which reflects the median time-adjusted sale price of comparable properties
          in neighborhood {neighborhood}.</p>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Property Tax Appeal — {subj_addr}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Georgia, "Times New Roman", serif; font-size: 13px;
         line-height: 1.65; color: #111; background: white; }}
  .page {{ max-width: 900px; margin: 0 auto; padding: 48px 48px; }}

  .letterhead {{ border-bottom: 2px solid #111; padding-bottom: 16px; margin-bottom: 24px; }}
  .letterhead h1 {{ font-size: 1.3em; font-weight: bold; letter-spacing: 0.02em; }}
  .letterhead .sub {{ font-size: 0.9em; color: #444; margin-top: 4px; }}

  .prop-block {{ background: #f7f7f7; border: 1px solid #ddd; padding: 14px 18px;
                margin-bottom: 24px; font-size: 0.92em; }}
  .prop-block table {{ width: 100%; border-collapse: collapse; }}
  .prop-block td {{ padding: 3px 12px 3px 0; vertical-align: top; }}
  .prop-block td:nth-child(odd) {{ font-weight: bold; width: 160px; color: #555;
                                   font-size: 0.88em; text-transform: uppercase;
                                   letter-spacing: 0.04em; }}

  .section {{ margin-bottom: 28px; }}
  .section h2 {{ font-size: 1.0em; font-weight: bold; text-transform: uppercase;
                letter-spacing: 0.06em; border-bottom: 1px solid #ccc;
                padding-bottom: 5px; margin-bottom: 12px; color: #222; }}
  .section p {{ margin-bottom: 8px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 0.88em; }}
  th {{ background: #222; color: white; padding: 7px 10px; text-align: left;
        font-size: 0.82em; text-transform: uppercase; letter-spacing: 0.04em; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #e8e8e8; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}

  .summary-grid {{ display: flex; gap: 20px; margin-top: 14px; flex-wrap: wrap; }}
  .summary-box {{ flex: 1; min-width: 160px; border: 1px solid #ddd; padding: 10px 14px; }}
  .summary-box .lbl {{ font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.06em;
                       color: #777; margin-bottom: 3px; }}
  .summary-box .val {{ font-size: 1.3em; font-weight: bold; }}

  .req {{ background: #f0f0f0; border-left: 4px solid #222; padding: 16px 20px; }}
  .req h2 {{ border-bottom-color: #999; }}

  .footnote {{ font-size: 0.78em; color: #777; margin-top: 28px;
              border-top: 1px solid #ddd; padding-top: 10px; }}

  @media print {{
    body {{ font-size: 11.5px; }}
    .page {{ padding: 24px 32px; max-width: none; }}
  }}
</style>
</head>
<body>
<div class="page">

  <div class="letterhead">
    <h1>Property Tax Appeal — Comparable Sales Analysis</h1>
    <div class="sub">Buncombe County 2026 Reappraisal &nbsp;·&nbsp; Prepared {today} &nbsp;·&nbsp; Submitted via <a href="https://tax.buncombenc.gov/" style="color:#444">tax.buncombenc.gov</a></div>
  </div>

  <div class="prop-block">
    <table>
      <tr>
        <td>Property Address</td><td><strong>{subj_addr}</strong></td>
        <td>PIN</td><td><strong>{subj_pin}</strong></td>
      </tr>
      <tr>
        <td>Neighborhood</td><td>{neighborhood}</td>
        <td>Lot Size</td><td>{subj_ac:.3f} acres</td>
      </tr>
      <tr>
        <td>2026 Assessed Value</td><td><strong>{fmt_money(subj_assessed)}</strong></td>
        <td>Building Value</td><td>{fmt_money(subj_bldg)}</td>
      </tr>
      {'<tr><td>Year Built</td><td>' + str(card["year_built"]) + '</td><td>Heated Sq Ft</td><td>' + f"{card['sq_ft']:,}" + ' sf</td></tr>' if card and card.get("year_built") else ''}
      {'<tr><td>Quality Grade</td><td>' + card["grade"] + ' (Custom)</td><td>Condition</td><td>' + card.get("condition","N") + ' (Normal)</td></tr>' if card and card.get("grade") else ''}
    </table>
  </div>

  <!-- ================================================ -->
  <div class="section">
    <h2>I. Basis for Appeal</h2>
    <p>The 2026 assessed value of <strong>{fmt_money(subj_assessed)}</strong> for the subject
    property is not supported by recent arm's-length market sales of comparable residential
    properties in Buncombe County neighborhood <strong>{neighborhood}</strong>.
    The following analysis presents {len(comps_scored)} qualified comparable sales from the
    county's public deed records since January 2022, derived from NC Revenue Stamps
    (NC Gen. Stat. § 105-228.30). Sale prices have been time-adjusted to the January 1, 2026
    appraisal date using the implied annual appreciation rate of {rate*100:.1f}%/yr
    back-calculated from this neighborhood's comp data.</p>
  </div>

  <!-- ================================================ -->
  <div class="section">
    <h2>II. Comparable Sales</h2>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Address</th>
          <th>PIN</th>
          <th>Sale Date</th>
          <th style="text-align:right">Raw Sale Price</th>
          <th style="text-align:right">Adj. to Jan 2026</th>
          <th style="text-align:right">2026 Assessed</th>
          <th style="text-align:right">Ratio (Assessed÷Adj.)</th>
          <th style="text-align:right">Bldg Value</th>
          <th style="text-align:right">Lot</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>

    <div class="summary-grid">
      {'<div class="summary-box"><div class="lbl">Median Adj. Sale Price</div><div class="val">' + fmt_money(median_price) + '</div></div>' if median_price else ''}
      {'<div class="summary-box"><div class="lbl">Median Assessment Ratio</div><div class="val">' + f"{median_ratio*100:.1f}%" + '</div></div>' if median_ratio else ''}
      {'<div class="summary-box"><div class="lbl">Subject Assessment</div><div class="val">' + fmt_money(subj_assessed) + '</div></div>'}
      {'<div class="summary-box"><div class="lbl">Above Median Comp Sale</div><div class="val">+' + fmt_money(subj_assessed - median_price) + '</div></div>' if median_price and median_price < subj_assessed else ''}
    </div>
  </div>

  {grade_html}

  {req_html}

  <div class="footnote">
    Sale prices derived from NC Revenue Stamps × $500 per NC Gen. Stat. § 105-228.30.
    Transfers with stamps below arm's-length threshold excluded per Buncombe County 2026
    Schedule of Values sales qualification criteria. "Adj. to Jan 2026" applies an implied
    {rate*100:.1f}%/yr annual appreciation rate (back-calculated from the median relationship
    between comparable properties' 2026 assessed values and their actual sale prices) to
    adjust older sales forward to the January 1, 2026 appraisal date. Ratios use the
    time-adjusted price. Assessment values from Buncombe County public GIS records
    (gis.buncombecounty.org), updated {today}.
  </div>

</div>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global COMP_SALE_START_DATE

    parser = argparse.ArgumentParser(description="Buncombe County 2026 property tax appeal tool")
    parser.add_argument("address", nargs="?", default=None,
                        help="Property address (will prompt if omitted)")
    parser.add_argument("--pin",  help="Look up by PIN instead of address")
    parser.add_argument("--since", default=COMP_SALE_START_DATE,
                        help=f"Earliest comp sale date YYYYMMDD (default: {COMP_SALE_START_DATE})")
    parser.add_argument("--output", default=None,
                        help="Output HTML filename (default: appeal_<address>.html)")
    # Property card details — all optional; omit to skip grade analysis section
    parser.add_argument("--grade",      default=None, help="Quality grade from property card (CUST, C, B, A, S, L, Q)")
    parser.add_argument("--sqft",       type=int, default=None, help="Heated sq ft from property card")
    parser.add_argument("--year-built", type=int, default=None, dest="year_built", help="Year built")
    parser.add_argument("--condition",  default="N", help="Condition code (R/G/N/F/P/U)")
    parser.add_argument("--pool-value", type=int, default=0, dest="pool_value", help="Pool assessed value (default: 0)")
    parser.add_argument("--yard-items",  type=int, default=0,    dest="yard_items",  help="Other yard item assessed values")
    parser.add_argument("--upload",      action="store_true",                        help="Generate clean upload document for the online appeal portal (no internal guidance)")
    parser.add_argument("--submission",  action="store_true",                        help=argparse.SUPPRESS)  # legacy alias for --upload
    parser.add_argument("--request",     type=int, default=None,                     help="Requested assessed value to include in the upload document")
    args = parser.parse_args()
    COMP_SALE_START_DATE = args.since

    print("Buncombe County Property Tax Appeal Tool")
    print("=" * 42)

    # Prompt for address if not provided via CLI
    address = args.address
    if not address and not args.pin:
        address = input("\n  Property address: ").strip()

    print(f"\n  Looking up property...", end=" ", flush=True)
    subject = lookup_by_pin(args.pin) if args.pin else lookup_by_address(address)
    if not subject:
        print("NOT FOUND")
        sys.exit(1)

    assessed = int(subject.get("TotalMarketValue") or 0)
    print(f"found")
    print(f"  Address:      {subject.get('HouseNumber','')} {subject.get('StreetName','')} {subject.get('StreetType','')}, {prop_city(subject)}")
    print(f"  PIN:          {subject.get('PIN')}")
    print(f"  Assessed:     ${assessed:,}")
    print(f"  Bldg Value:   ${int(subject.get('BuildingValue') or 0):,}")
    print(f"  Neighborhood: {subject.get('NeighborhoodCode')} (Area {subject.get('AppraisalArea','')})")
    print(f"  Lot:          {float(subject.get('Acreage') or 0):.3f} ac")

    print(f"\n  Finding comparable sales since {COMP_SALE_START_DATE}...", end=" ", flush=True)
    comps = find_comp_candidates(subject)
    subj_pin = subject.get("PIN")
    comps = [c for c in comps if c.get("PIN") != subj_pin]

    # Calculate implied appreciation rate from comps before scoring
    rate = implied_annual_rate(comps)

    for c in comps:
        comp_assessed  = int(c.get("TotalMarketValue") or 0)
        c["_score"]     = score_comp(subject, c)
        c["_ratio"]     = assessment_ratio(comp_assessed, c["_sale_price"])
        c["_years"]     = years_before_appraisal(c.get("DeedDate"))
        c["_adj_price"] = time_adjusted_price(c["_sale_price"], c.get("DeedDate"), rate)
        c["_adj_ratio"] = assessment_ratio(comp_assessed, c["_adj_price"])

    comps_scored = sorted(comps, key=lambda c: c["_score"], reverse=True)
    print(f"{len(comps_scored)} comps found (filtered & deduplicated)")

    if not comps_scored:
        print("\n  No comps found. Try --since 20200101 to widen date range.")
        sys.exit(0)

    # Console summary
    print(f"\n  Implied market appreciation rate: {rate*100:.1f}%/yr (back-calculated from comp assessed÷sale ratios)")
    print(f"\n  {'Address':<38} {'Date':<12} {'Sale':>10} {'Adj.2026':>10} {'Assessed':>10} {'Ratio':>8} {'Score':>6}")
    print(f"  {'-'*38} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*6}")
    for c in comps_scored:
        r = c["_adj_ratio"]
        rs = f"{r*100:.1f}%" if r else "  —"
        is_post = c.get("_years", 1) == 0
        adj_str = "post-appr" if is_post else fmt_money(c["_adj_price"])
        print(f"  {prop_address(c):<38} {fmt_date(c.get('DeedDate')):<12} "
              f"{fmt_money(c['_sale_price']):>10} "
              f"{adj_str:>10} "
              f"{fmt_money(int(c.get('TotalMarketValue') or 0)):>10} "
              f"{rs:>8} {c['_score']:>6}")

    adj_prices = [c["_adj_price"] for c in comps_scored]
    adj_ratios = [c["_adj_ratio"] for c in comps_scored if c["_adj_ratio"]]
    if adj_prices:
        median_p = sorted(adj_prices)[len(adj_prices)//2]
        print(f"\n  Median adj. comp sale price: ${median_p:,}")
        if median_p < assessed:
            print(f"  Your assessment is ${assessed - median_p:,} ({(assessed-median_p)/median_p*100:.1f}%) above the median adj. comp sale")
    if adj_ratios:
        median_r = sorted(adj_ratios)[len(adj_ratios)//2]
        print(f"  Median assessment ratio (adj.): {median_r*100:.1f}%  (100% = assessed exactly at adj. sale price)")

    upload_mode = args.upload or args.submission   # --submission is a legacy alias

    print(f"\n  Generating {'upload document' if upload_mode else 'report'}...")

    # Auto-name output file from address if not specified
    safe = (address or subject.get("Address","report")).replace(" ", "_").replace(",","").lower()
    if args.output:
        out = Path(args.output)
    elif upload_mode:
        out = Path(f"appeal_{safe}_upload.html")
    else:
        out = Path(f"appeal_{safe}.html")

    # Only run grade analysis if at least grade + sqft + year_built are provided
    card = None
    if args.grade and args.sqft and args.year_built:
        card = {
            "grade":      args.grade,
            "sq_ft":      args.sqft,
            "year_built": args.year_built,
            "condition":  args.condition,
            "pool_value": args.pool_value,
            "yard_items": args.yard_items,
        }

    if upload_mode:
        generate_submission_html(subject, comps_scored, out, card=card,
                                 requested_value=args.request, rate=rate)
        print(f"  Saved (upload doc): {out.resolve()}")
        print(f"  → Print to PDF from your browser, then attach to your online appeal at tax.buncombenc.gov")
    else:
        generate_html(subject, comps_scored, out, card=card, rate=rate)
        print(f"  Saved (full report): {out.resolve()}")
        print(f"  → Run with --upload to generate the clean version to attach to your online appeal")
    open_cmd = "start" if sys.platform == "win32" else "open"
    print(f"  Open:  {open_cmd} '{out.resolve()}'")
    print()


if __name__ == "__main__":
    main()
