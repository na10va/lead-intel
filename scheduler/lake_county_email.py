"""
scheduler/lake_county_email.py — Sends the monthly delinquent list request to Karen
at the Lake County Auditor's office on the first Monday of each month.

Contact: Karen Potter — kpotter@lakecountyohio.gov
From: info@thevnagroup.com (LAKE_COUNTY_FROM_EMAIL in .env)
Schedule: First Monday of every month at 7:00 AM EST, starting May 4 2026
          (April 2026 data already ingested manually)

Karen replies with the delinquent taxpayer report (CSV, Excel, or PDF).
When the file arrives, ingest it with:
    python agents/tax_lien_agent.py --county lake --ingest-file <path>

CLI:
    python scheduler/lake_county_email.py          # send now (test/manual trigger)
"""

import os

from dotenv import load_dotenv

from routing.notify import send_email
from utils.logger import get_logger

load_dotenv()

log = get_logger("scheduler.lake_county_email")

KAREN_EMAIL = "kpotter@lakecountyohio.gov"
FROM_EMAIL = os.getenv("LAKE_COUNTY_FROM_EMAIL", "info@thevnagroup.com")

_SUBJECT = "Monthly Delinquent Property List Request — Direct Home Solutions LLC"

_BODY = """\
<p>Hi Karen,</p>

<p>This is our monthly request for the updated delinquent taxpayer list for Lake County
properties. Could you please send the most recent list at your earliest convenience?
We are happy to receive it in whatever format is easiest for you (CSV, Excel, or PDF).</p>

<p>Thank you for your continued help!</p>

<p>Best regards,<br>
Nicholas Anton<br>
Direct Home Solutions LLC / The VNA Group<br>
Phone: (216) 412-9380<br>
Email: info@thevnagroup.com</p>
"""


def send_lake_county_email() -> bool:
    """Send the monthly delinquent list request to Karen at Lake County Auditor's office."""
    log.info(f"Sending monthly delinquent list request to {KAREN_EMAIL}")
    success = send_email(
        to=KAREN_EMAIL,
        subject=_SUBJECT,
        html_body=_BODY,
        from_email=FROM_EMAIL,
    )
    if success:
        log.info("Lake County monthly email sent successfully")
    else:
        log.error("Failed to send Lake County monthly email — check SendGrid credentials")
    return success


if __name__ == "__main__":
    send_lake_county_email()
