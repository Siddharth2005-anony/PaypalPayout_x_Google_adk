"""SMTP notifications for PayPal payout failures.

The Gmail address used for a follow-up is local metadata. It is never sent to
PayPal as part of a payout item.
"""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from typing import Any, Iterable

from dotenv import load_dotenv

load_dotenv()

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _smtp_config() -> tuple[str, int, str, str, str] | None:
    """Return SMTP settings, or None when SMTP is not configured."""
    username = _as_text(os.getenv("SMTP_USERNAME") or os.getenv("GMAIL_ADDRESS"))
    password = _as_text(os.getenv("SMTP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD"))
    if not username or not password:
        return None

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        port = 587

    host = _as_text(os.getenv("SMTP_HOST")) or "smtp.gmail.com"
    from_address = _as_text(os.getenv("SMTP_FROM_EMAIL")) or username
    return host, port, username, password, from_address


def _valid_email(value: Any) -> bool:
    return bool(EMAIL_PATTERN.match(_as_text(value)))


def _send_email(recipient: str, subject: str, body: str) -> dict[str, Any]:
    """Send one email and return a safe, structured result."""
    recipient = _as_text(recipient)
    if not _valid_email(recipient):
        return {"status": "skipped", "reason": "No valid recipient email address."}

    config = _smtp_config()
    if config is None:
        return {
            "status": "skipped",
            "reason": (
                "SMTP is not configured. Set SMTP_USERNAME and SMTP_PASSWORD "
                "(or GMAIL_ADDRESS and GMAIL_APP_PASSWORD) in .env."
            ),
        }

    host, port, username, password, from_address = config
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return {"status": "sent", "recipient": recipient}
    except (OSError, smtplib.SMTPException) as exc:
        # Do not include credentials or the full SMTP diagnostic in the agent response.
        return {"status": "failed", "recipient": recipient, "reason": str(exc)}


def _recipient_lookup(recipients: Iterable[dict[str, Any]] | None) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for recipient in recipients or []:
        if not isinstance(recipient, dict):
            continue
        email = _as_text(
            recipient.get("email")
            or recipient.get("follow_up_email")
            or recipient.get("gmail")
        )
        if not _valid_email(email):
            continue
        for key in ("sender_item_id", "payout_item_id", "receiver"):
            value = _as_text(recipient.get(key))
            if value:
                lookup[value] = email
    return lookup


def send_failure_followups(
    failures: Iterable[dict[str, Any]],
    recipients: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Email each failed payout's recipient when a follow-up address is known."""
    lookup = _recipient_lookup(recipients)
    results: list[dict[str, Any]] = []

    for failure in failures:
        if not isinstance(failure, dict):
            continue
        recipient = _as_text(
            failure.get("follow_up_email")
            or lookup.get(_as_text(failure.get("sender_item_id")))
            or lookup.get(_as_text(failure.get("payout_item_id")))
            or lookup.get(_as_text(failure.get("recipient")))
        )
        paypal_recipient = _as_text(failure.get("recipient")) or "the PayPal recipient"
        reason = _as_text(failure.get("reason")) or "PayPal reported that this payout failed."
        subject = "Action needed: your PayPal payout could not be completed"
        body = (
            f"Hello,\n\n"
            f"Your payout to PayPal account {paypal_recipient} could not be completed.\n\n"
            f"Reason reported by PayPal: {reason}\n\n"
            "Please verify your PayPal email address or contact the sender if you believe "
            "this failure is incorrect.\n"
        )
        result = _send_email(recipient, subject, body)
        result.update(
            {
                "sender_item_id": failure.get("sender_item_id"),
                "paypal_recipient": failure.get("recipient"),
            }
        )
        results.append(result)

    return {
        "attempted": len(results),
        "sent": sum(result["status"] == "sent" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "details": results,
    }


def send_sender_failure_followup(reason: str) -> dict[str, Any]:
    """Notify the operator when PayPal rejects a submission before item tracking."""
    recipient = (
        _as_text(os.getenv("FOLLOW_UP_USER_EMAIL"))
        or _as_text(os.getenv("USER_EMAIL"))
        or _as_text(os.getenv("GMAIL_ADDRESS"))
        or _as_text(os.getenv("SMTP_USERNAME"))
    )
    subject = "PayPal payout submission failed"
    body = (
        "Hello,\n\n"
        "The PayPal payout batch could not be submitted.\n\n"
        f"Reason reported by PayPal: {_as_text(reason) or 'Unknown PayPal error.'}\n\n"
        "Please resolve the issue and submit the payout again if appropriate.\n"
    )
    return _send_email(recipient, subject, body)


def paypal_items_only(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove local follow-up metadata before sending items to PayPal."""
    allowed = {"recipient_type", "receiver", "amount", "note", "sender_item_id"}
    return [
        {key: value for key, value in item.items() if key in allowed}
        for item in items
        if isinstance(item, dict)
    ]


def validate_payout_items(items: Any) -> str | None:
    """Return an explicit user-facing validation message, if any."""
    if not isinstance(items, list) or not items:
        return "Please provide at least one recipient with a PayPal email and amount."

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return f"Payout item {index} is not a valid object."
        if not _as_text(item.get("receiver")):
            return f"Payout item {index} is missing the recipient's PayPal email address."
        amount = item.get("amount")
        if not isinstance(amount, dict) or not _as_text(amount.get("value")):
            return f"Payout item {index} is missing an amount."
    return None


def follow_up_recipients_from_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the local metadata needed to notify recipients after tracking."""
    return [
        {
            "sender_item_id": item.get("sender_item_id"),
            "receiver": item.get("receiver"),
            "email": item.get("follow_up_email"),
        }
        for item in items
        if isinstance(item, dict) and _valid_email(item.get("follow_up_email"))
    ]
