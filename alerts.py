import os
import requests


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _split_emails(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def send_email_resend(subject: str, text: str, html: str | None = None) -> tuple[bool, str]:
    """
    Uses Resend HTTP API. No extra dependencies (requests already in your requirements).
    Env:
      RESEND_API_KEY
      ALERT_FROM_EMAIL   e.g. "SqueezeBot <alerts@yourdomain.com>"
      ALERT_TO_EMAILS    comma-separated list
    """
    api_key = _env("RESEND_API_KEY")
    from_email = _env("ALERT_FROM_EMAIL")
    to_emails = _split_emails(_env("ALERT_TO_EMAILS"))

    if not api_key:
        return False, "RESEND_API_KEY not set"
    if not from_email:
        return False, "ALERT_FROM_EMAIL not set"
    if not to_emails:
        return False, "ALERT_TO_EMAILS not set"

    payload = {
        "from": from_email,
        "to": to_emails,
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )

    if r.status_code >= 300:
        return False, f"Resend error {r.status_code}: {r.text[:200]}"
    return True, "sent"
