"""
zillow/owner_notify.py — Sends Zillow Deal Finder results to the owner only.

NEVER routes to VA. NEVER enriches with Skip Sherpa.
Sends one daily email at 7:50 AM EST with all deals ≤75% of comp ARV.

Email includes a clean table sorted by % of comp ARV (best deals first).
ARV conflict listings are flagged with a warning note.
"""

import os

from routing.notify import send_email
from zillow.deal_scorer import sort_deals
from utils.logger import get_logger

log = get_logger("zillow.owner_notify")

OWNER_EMAIL = os.getenv("OWNER_EMAIL")


def send_deal_alert(deals: list[dict]) -> bool:
    """Send the daily Zillow Deal Finder email to the owner.

    Args:
        deals: List of scored zillow_deals dicts (already filtered to ≤75% ARV).

    Returns True if email sent successfully.
    """
    if not deals:
        log.info("No deals to report today — skipping Zillow email")
        return True

    sorted_deals = sort_deals(deals)
    subject = f"Zillow Deal Finder — {len(sorted_deals)} deal(s) found today"
    body = _build_email(sorted_deals)

    result = send_email(to=OWNER_EMAIL, subject=subject, html_body=body)
    if result:
        log.info(f"Zillow deal alert sent — {len(sorted_deals)} deals")
    return result


def _build_email(deals: list[dict]) -> str:
    """Build the HTML email with a sortable deals table."""
    rows = ""
    for d in deals:
        conflict_note = " ⚠️ ARV Conflict" if d.get("arv_conflict") else ""
        label = d.get("label", "")
        label_display = {
            "Deep Value": "🔥 Deep Value",
            "On Target": "✅ On Target",
            "Worth a Look": "👀 Worth a Look",
        }.get(label, label)

        rows += f"""
<tr>
  <td><a href="{d.get('zillow_url', '#')}">{d.get('address', '')}</a></td>
  <td>${d.get('list_price', 0):,}</td>
  <td>${d.get('zestimate_arv') or 0:,}</td>
  <td>${d.get('comp_arv') or 0:,}{conflict_note}</td>
  <td><strong>{d.get('pct_of_comp_arv', '?')}%</strong></td>
  <td>{d.get('beds', '?')}</td>
  <td>{d.get('baths', '?')}</td>
  <td>{d.get('sqft') or '?':,}</td>
  <td>{d.get('days_on_market', '?')}</td>
  <td>{d.get('listing_type', '').title()}</td>
  <td>{label_display}</td>
</tr>"""

    return f"""
<h2>Zillow Deal Finder — {len(deals)} listing(s) at ≤75% ARV</h2>
<p>Sorted by % of comp ARV (best deals first). <strong>Owner calls only — do not send to VA.</strong></p>
<table border="1" cellpadding="6">
<thead>
<tr>
  <th>Address</th><th>List Price</th><th>Zestimate ARV</th><th>Comp ARV</th>
  <th>% of Comp ARV</th><th>Beds</th><th>Baths</th><th>SqFt</th>
  <th>Days on Market</th><th>Type</th><th>Label</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
<p style="color:#999;font-size:12px;">
⚠️ ARV Conflict = Zestimate and comp ARV diverge by more than 15%. Review manually before calling.
</p>
"""
