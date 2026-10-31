import os
from typing import Any

import requests
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models import LiteLlm

from email_followup import send_failure_followups

load_dotenv()

PAYPAL_BASE_URL = "https://api-m.sandbox.paypal.com"
TIMEOUT_SECONDS = 30


chap = LiteLlm(
    model = "openai/gpt-6-luna",
    api_key = os.getenv("OPENAI_API_KEY")
)

def _get_access_token() -> str:
    """Get a PayPal Sandbox OAuth access token using the app credentials."""
    client_id = os.getenv("PAYPAL_CLIENT_ID")
    client_secret = os.getenv("PAYPAL_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET in the .env file."
        )

    response = requests.post(
        f"{PAYPAL_BASE_URL}/v1/oauth2/token",
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
        data={"grant_type": "client_credentials"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    token = response.json().get("access_token")

    if not token:
        raise RuntimeError("PayPal did not return an access token.")

    return token


def track_payout_batch(
    batch_id: str,
    follow_up_recipients: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Retrieve a PayPal Sandbox payout batch and summarize item outcomes.

    Args:
        batch_id: The PayPal-generated payout_batch_id returned when the
                  payout batch was submitted. Do not use sender_batch_id.
    """
    batch_id = (batch_id or "").strip()
    if not batch_id:
        return {"status": "error", "message": "Please provide a payout batch ID."}

    try:
        token = _get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Fetch all pages so counts are not based on only the first page.
        first_response = requests.get(
            f"{PAYPAL_BASE_URL}/v1/payments/payouts/{batch_id}",
            headers=headers,
            params={"page": 1, "page_size": 100, "total_required": "true"},
            timeout=TIMEOUT_SECONDS,
        )

        if first_response.status_code == 404:
            return {
                "status": "error",
                "message": (
                    "PayPal could not find this batch ID in Sandbox. "
                    "Check that you supplied the PayPal-generated payout_batch_id "
                    "and that the app credentials belong to the same Sandbox."
                ),
            }

        first_response.raise_for_status()
        data = first_response.json()
        batch_header = data.get("batch_header", {})
        items = list(data.get("items") or [])
        total_pages = int(data.get("total_pages") or 1)

        for page in range(2, total_pages + 1):
            page_response = requests.get(
                f"{PAYPAL_BASE_URL}/v1/payments/payouts/{batch_id}",
                headers=headers,
                params={
                    "page": page,
                    "page_size": 100,
                    "total_required": "true",
                },
                timeout=TIMEOUT_SECONDS,
            )
            page_response.raise_for_status()
            items.extend(page_response.json().get("items") or [])

        successes = []
        failures = []
        other_statuses = []

        for item in items:
            transaction_status = str(item.get("transaction_status") or "UNKNOWN").upper()
            payout_item = item.get("payout_item") or {}
            errors = item.get("errors") or {}
            error_details = errors.get("details") or []

            # Build a reason only from details returned by PayPal.
            reasons = []
            if errors.get("name"):
                reasons.append(str(errors["name"]))
            if errors.get("message"):
                reasons.append(str(errors["message"]))
            for detail in error_details:
                if not isinstance(detail, dict):
                    continue
                issue = detail.get("issue")
                description = detail.get("description")
                if issue and issue not in reasons:
                    reasons.append(str(issue))
                if description and description not in reasons:
                    reasons.append(str(description))

            entry = {
                "recipient": payout_item.get("receiver"),
                "sender_item_id": payout_item.get("sender_item_id"),
                "transaction_id": item.get("transaction_id"),
                "status": transaction_status,
            }

            if transaction_status == "SUCCESS":
                successes.append(entry)
            elif transaction_status == "FAILED":
                entry["reason"] = "; ".join(reasons) if reasons else (
                    "PayPal did not provide a failure reason in the batch response. "
                    "Inspect the payout item details for more information."
                )
                entry["payout_item_id"] = item.get("payout_item_id")
                failures.append(entry)
            else:
                entry["reason"] = "; ".join(reasons) if reasons else None
                other_statuses.append(entry)

        result = {
            "status": "success",
            "batch_id": batch_header.get("payout_batch_id", batch_id),
            "batch_status": batch_header.get("batch_status", "UNKNOWN"),
            "total_items_returned": len(items),
            "success_count": len(successes),
            "failure_count": len(failures),
            "other_count": len(other_statuses),
            "successes": successes,
            "failures": failures,
            "other_items": other_statuses,
            "note": (
                "Counts reflect the statuses PayPal returned at lookup time. "
                "A batch may still be processing, so track it again later if "
                "items remain pending or in another nonterminal state."
            ),
        }

        if failures:
            result["email_notifications"] = send_failure_followups(
                failures,
                follow_up_recipients,
            )
        return result

    except requests.HTTPError as exc:
        response = exc.response
        detail = ""
        if response is not None:
            try:
                payload = response.json()
                detail = payload.get("message") or payload.get("name") or str(payload)
            except ValueError:
                detail = response.text[:500]
        return {
            "status": "error",
            "message": f"PayPal API request failed: {detail or str(exc)}",
            "http_status": response.status_code if response is not None else None,
        }
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        return {"status": "error", "message": f"Could not track payout batch: {exc}"}


root_agent = Agent(
    model=chap,
    name="batch_tracking_agent",
    description="Tracks PayPal Sandbox payout batches and explains failed payout items.",
    instruction=(
        "Help users track existing PayPal Sandbox payout batches. When the user "
        "provides a batch ID and asks to track it, call track_payout_batch with the "
        "PayPal-generated payout_batch_id. Do not invent statuses or failure reasons. "
        "Summarize the batch status, success count, failure count, and other/nonterminal "
        "count. For each failed item, report the recipient or sender_item_id when available "
        "and the reason returned by PayPal. If PayPal provides no reason, say so explicitly. "
        "If follow_up_recipients metadata is available from the original payout submission, "
        "pass it to track_payout_batch. The tool will send one SMTP follow-up email per "
        "failed item with a known address; report sent, skipped, and failed email counts. "
        "Do not submit, retry, cancel, or otherwise change payouts."
    ),
    tools=[track_payout_batch],
)
