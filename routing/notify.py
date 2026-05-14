"""
routing/notify.py — Sends SMS and email alerts.

SMS (Twilio): Tier A leads and source alerts only.
Email: Tries SendGrid first; falls back to SMTP (Gmail or any SMTP server) if
       SendGrid is unavailable or over its monthly limit.

Environment variables:
    SendGrid:  SENDGRID_API_KEY, SENDGRID_FROM_EMAIL
    SMTP:      SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
               SMTP_USER, SMTP_PASSWORD
    Twilio:    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
    Owner:     OWNER_PHONE, OWNER_EMAIL
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from twilio.rest import Client as TwilioClient

from utils.logger import get_logger

load_dotenv()

log = get_logger("routing.notify")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
OWNER_PHONE        = os.getenv("OWNER_PHONE")

SENDGRID_API_KEY   = os.getenv("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL")

SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

OWNER_EMAIL   = os.getenv("OWNER_EMAIL")


def send_sms(message: str, to: str = None) -> bool:
    """Send an SMS via Twilio to the owner (or a custom number)."""
    to = to or OWNER_PHONE

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, to]):
        log.error("Twilio credentials incomplete — SMS not sent")
        return False

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            body=message,
            from_=TWILIO_FROM_NUMBER,
            to=to,
        )
        log.info(f"SMS sent — SID: {msg.sid}")
        _log_sms_cost()
        return True
    except Exception as e:
        log.error(f"Failed to send SMS: {e}")
        return False


def send_tier_a_sms(lead: dict) -> bool:
    """Send the standard Tier A lead SMS alert to the owner."""
    message = (
        f"[TIER A LEAD] {lead.get('owner_name', 'Unknown')} | "
        f"{lead.get('property_address', 'Unknown address')} | "
        f"{lead.get('county', '')}, {lead.get('state', '')} | "
        f"Score: {lead.get('score', '?')} | "
        f"Source: {lead.get('source_type', '?')}"
    )
    return send_sms(message)


def send_email(to: str, subject: str, html_body: str, from_email: str = None) -> bool:
    """Send an email, trying SendGrid first and falling back to SMTP.

    Args:
        to:         Recipient email address.
        subject:    Email subject line.
        html_body:  HTML content of the email.
        from_email: Override sender address.

    Returns True on success, False if both transports fail.
    """
    if not to:
        log.error("send_email called with no recipient")
        return False

    # --- Try SendGrid ---
    if SENDGRID_API_KEY:
        ok = _send_via_sendgrid(to, subject, html_body, from_email)
        if ok:
            return True
        log.warning("SendGrid failed — falling back to SMTP")

    # --- Fall back to SMTP ---
    return _send_via_smtp(to, subject, html_body, from_email)


def _send_via_sendgrid(to: str, subject: str, html_body: str, from_email: str = None) -> bool:
    sender = from_email or SENDGRID_FROM_EMAIL
    if not all([SENDGRID_API_KEY, sender]):
        return False
    try:
        message = Mail(
            from_email=sender,
            to_emails=to,
            subject=subject,
            html_content=html_body,
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        log.info(f"Email sent via SendGrid to {to} — status {response.status_code}")
        _log_email_cost()
        return True
    except Exception as e:
        log.warning(f"SendGrid error: {e}")
        return False


def _send_via_smtp(to: str, subject: str, html_body: str, from_email: str = None) -> bool:
    sender = from_email or SMTP_USER or SENDGRID_FROM_EMAIL
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD]):
        log.error("SMTP credentials not configured — email not sent. "
                  "Set SMTP_USER and SMTP_PASSWORD in .env")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(sender, to, msg.as_string())

        log.info(f"Email sent via SMTP ({SMTP_HOST}) to {to}")
        _log_email_cost()
        return True
    except Exception as e:
        log.error(f"SMTP send failed: {e}")
        return False


def _log_sms_cost() -> None:
    try:
        from db.client import insert_row
        insert_row("api_costs", {
            "service":  "twilio",
            "lead_id":  None,
            "cost_usd": 0.0075,
            "result":   "success",
        })
    except Exception as e:
        log.warning(f"Could not log SMS cost: {e}")


def _log_email_cost() -> None:
    try:
        from db.client import insert_row
        insert_row("api_costs", {
            "service":  "sendgrid",
            "lead_id":  None,
            "cost_usd": 0.001,
            "result":   "success",
        })
    except Exception as e:
        log.warning(f"Could not log email cost: {e}")
