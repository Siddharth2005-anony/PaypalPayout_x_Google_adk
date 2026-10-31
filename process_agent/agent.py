import os, uuid, re
from io import BytesIO, StringIO
from urllib.parse import urlparse, parse_qs

import requests

import pandas as pd
from dotenv import load_dotenv
from google.adk.agents.llm_agent import Agent
from google.adk.tools import ToolContext
from openai import OpenAI
from pydantic import BaseModel

from email_followup import (
    follow_up_recipients_from_items,
    paypal_items_only,
    send_sender_failure_followup,
    validate_payout_items,
)
from batch_tracking.agent import track_payout_batch

load_dotenv()

from paypalpayoutssdk.core import (PayPalHttpClient,SandboxEnvironment)
from paypalpayoutssdk.payouts import PayoutsPostRequest


# Structured output schema for the OpenAI Responses API
class Amount(BaseModel):
    value: str
    currency: str


class PayPalItem(BaseModel):
    recipient_type: str
    receiver: str
    amount: Amount
    note: str
    sender_item_id: str
    follow_up_email: str | None = None


class PayoutBatch(BaseModel):
    items: list[PayPalItem]


def read_excel_from_upload(tool_context: ToolContext) -> dict:
    """Read the user's uploaded Excel workbook and convert rows into PayPal payout items."""
    user_content = getattr(tool_context, "user_content", None)
    parts = getattr(user_content, "parts", None) if user_content else None

    if not parts:
        return {"status": "error", "message": "No Excel file uploaded."}

    excel_part = next(
        (
            part
            for part in parts
            if getattr(getattr(part, "inline_data", None), "mime_type", None)
            in {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
            }
        ),
        None,
    )

    if excel_part is None:
        return {
            "status": "error",
            "message": "Please upload an .xlsx or .xls spreadsheet.",
        }

    try:
        excel_bytes = excel_part.inline_data.data
        df = pd.read_excel(BytesIO(excel_bytes))

        if df.empty:
            return {"status": "error", "message": "The uploaded workbook is empty."}

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {
                "status": "error",
                "message": "OPENAI_API_KEY is not configured.",
            }

        client = OpenAI(api_key=api_key)
        response = client.responses.parse(
            model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
            input=[
                {
                    "role": "system",
                    "content": (
                        "Convert spreadsheet rows into PayPal payout items. "
                        "Copy recipient email addresses and amounts exactly; "
                        "never invent or modify them. Each row should produce one item. "
                        "Use recipient_type EMAIL. Use USD as the currency. "
                        "Set note to 'Testing payout API'. "
                        "Use a unique sender_item_id such as fork_1, fork_2, etc. "
                        "Return only the structured output matching the schema."
                    ),
                },
                {"role": "user", "content": df.to_csv(index=False)},
            ],
            text_format=PayoutBatch,
        )

        batch = response.output_parsed
        if batch is None:
            return {
                "status": "error",
                "message": "The payout conversion returned no structured result.",
            }

        # Set deterministic fields in Python rather than relying on the LLM.
        for i, item in enumerate(batch.items, start=1):
            item.recipient_type = "EMAIL"
            item.amount.currency = "USD"
            item.sender_item_id = f"fork_{i}"
            if not item.note:
                item.note = "Testing payout API"

            # Gmail is notification metadata, not a PayPal payout field.
            row = df.iloc[i - 1] if i <= len(df) else None
            if row is not None:
                for column in df.columns:
                    normalized = str(column).strip().lower().replace(" ", "_")
                    if normalized in {
                        "gmail",
                        "gmail_address",
                        "follow_up_email",
                        "notification_email",
                    }:
                        value = row[column]
                        if pd.notna(value):
                            item.follow_up_email = str(value).strip()
                        break

        validation_error = validate_payout_items(
            [item.model_dump() for item in batch.items]
        )
        if validation_error:
            return {
                "status": "error",
                "message": f"Please make the spreadsheet explicit: {validation_error}",
            }

        return {
            "status": "success",
            "items": [item.model_dump() for item in batch.items],
            "count": len(batch.items),
        }

    except Exception as exc:
        return {
            "status": "error",
            "message": f"Could not process the uploaded workbook: {exc}",
        }



     


def read_google_sheet(url: str) -> dict:
    """
    Read a publicly accessible Google Sheet URL and convert its rows into
    the same PayPal payout-item structure used by read_excel_from_upload.

    Expected columns are name, email, and amount. A Gmail/follow-up email
    column is optional.
    """
    if not isinstance(url, str) or not url.strip():
        return {
            "status": "error",
            "message": "Please provide a public Google Sheets URL."
        }

    url = url.strip()

    try:
        parsed = urlparse(url)

        if (
            parsed.scheme != "https"
            or parsed.hostname != "docs.google.com"
            or parsed.path.split("/")[1:2] != ["spreadsheets"]
        ):
            return {
                "status": "error",
                "message": (
                    "Invalid URL. Please paste a Google Sheets URL beginning "
                    "with https://docs.google.com/spreadsheets/..."
                )
            }

        match = re.fullmatch(
            r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?",
            parsed.path
        )
        if not match:
            return {
                "status": "error",
                "message": "Could not extract the spreadsheet ID from the URL."
            }

        spreadsheet_id = match.group(1)

        # Google Sheets commonly stores gid in either the query or URL fragment.
        query_params = parse_qs(parsed.query)
        fragment_params = parse_qs(parsed.fragment)
        gid = (
            query_params.get("gid", [None])[0]
            or fragment_params.get("gid", [None])[0]
            or "0"
        )

        if not str(gid).isdigit():
            return {
                "status": "error",
                "message": "The worksheet gid in the URL is invalid."
            }

    except Exception:
        return {
            "status": "error",
            "message": "Could not parse the Google Sheets URL."
        }

    export_url = (
        f"https://docs.google.com/spreadsheets/d/"
        f"{spreadsheet_id}/export"
    )

    try:
        response = requests.get(
            export_url,
            params={"format": "csv", "gid": gid},
            timeout=20,
            allow_redirects=True,
            headers={"User-Agent": "PayPal-Payout-Agent/1.0"}
        )
        response.raise_for_status()

        # A private sheet may return an HTML login/permission page instead of CSV.
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/csv" not in content_type and "application/octet-stream" not in content_type:
            return {
                "status": "error",
                "message": (
                    "Could not read the spreadsheet as CSV. Make sure its "
                    "Google Sheets sharing setting is 'Anyone with the link' "
                    "with Viewer access, and that the URL points to the correct tab."
                )
            }

        max_bytes = 5 * 1024 * 1024
        if len(response.content) > max_bytes:
            return {
                "status": "error",
                "message": "The spreadsheet export exceeds the 5 MB limit."
            }

        df = pd.read_csv(StringIO(response.content.decode("utf-8-sig")))

        if df.empty:
            return {
                "status": "error",
                "message": "The Google Spreadsheet is empty."
            }

        if len(df) > 1000:
            return {
                "status": "error",
                "message": (
                    "The spreadsheet contains more than 1,000 rows. "
                    "Please split it into smaller payout batches."
                )
            }

        # Use the same structured conversion approach as the existing Excel tool.
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {
                "status": "error",
                "message": "OPENAI_API_KEY is not configured."
            }

        client = OpenAI(api_key=api_key)
        response = client.responses.parse(
            model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
            input=[
                {
                    "role": "system",
                    "content": (
                        "Convert spreadsheet rows into PayPal payout items. "
                        "Copy recipient email addresses and amounts exactly; "
                        "never invent or modify them. Each non-empty row should "
                        "produce one item. The spreadsheet should contain a name, "
                        "email/recipient, and amount column. Use recipient_type "
                        "EMAIL. Use USD as the currency. Set note to "
                        "'Testing payout API'. Use unique sender_item_id values "
                        "such as fork_1, fork_2, etc. If the sheet has a Gmail, "
                        "gmail_address, follow_up_email, or notification_email "
                        "column, do not put it in the PayPal item; it is optional "
                        "notification metadata. Return only the structured output "
                        "matching the schema."
                    ),
                },
                {"role": "user", "content": df.to_csv(index=False)},
            ],
            text_format=PayoutBatch,
        )

        batch = response.output_parsed
        if batch is None:
            return {
                "status": "error",
                "message": "The Google Sheet conversion returned no structured result."
            }

        for i, item in enumerate(batch.items, start=1):
            item.recipient_type = "EMAIL"
            item.amount.currency = "USD"
            item.sender_item_id = f"fork_{i}"
            if not item.note:
                item.note = "Testing payout API"

            row = df.iloc[i - 1] if i <= len(df) else None
            if row is not None:
                for column in df.columns:
                    normalized = str(column).strip().lower().replace(" ", "_")
                    if normalized in {
                        "gmail",
                        "gmail_address",
                        "follow_up_email",
                        "notification_email",
                    }:
                        value = row[column]
                        if pd.notna(value):
                            item.follow_up_email = str(value).strip()
                        break

        validation_error = validate_payout_items(
            [item.model_dump() for item in batch.items]
        )
        if validation_error:
            return {
                "status": "error",
                "message": f"Please make the Google Sheet explicit: {validation_error}"
            }

        return {
            "status": "success",
            "items": [item.model_dump() for item in batch.items],
            "count": len(batch.items),
            "source": "google_sheets"
        }

    except requests.RequestException:
        return {
            "status": "error",
            "message": (
                "Could not access the Google Sheet. Confirm that the link is "
                "publicly accessible to anyone with the link."
            )
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Could not process the Google Sheet: {exc}"
        }


def payout_folks(items: list[dict]) -> dict:
    """Submit the exact `items` list returned by read_excel_from_upload to PayPal Sandbox.
    - If the toold natural_language_processing is executed, extract the list from that.

    Args:
        items: The `items` array from the successful read_excel_from_upload tool result. Similarly for natural_language_processing
               Pass the array itself, not the entire result dictionary.
    """
    if not isinstance(items, list) or not items:
        return {
            "status": "error",
            "message": "No payout items received. Run read_excel_from_upload or natural_language_processing first and pass its returned 'items' list."
        }

    client_id = os.getenv("PAYPAL_CLIENT_ID")
    client_server = os.getenv("PAYPAL_CLIENT_SECRET")

    if not client_id or not client_server:
        return {
            "status": "error",
            "message": (
                "Paypal credentials are missing. please check your .env file."
            )
        }

    environment = SandboxEnvironment(
        client_id = client_id,
        client_secret=client_server
    )

    client = PayPalHttpClient(environment=environment)

    print("PayPal Sandbox client initialized successfully!")

    batch_id = "test_" + uuid.uuid4().hex[:20]

    paypal_items = paypal_items_only(items)
    if len(paypal_items) != len(items):
        return {
            "status": "error",
            "message": "Each payout item must be a dictionary.",
        }

    body = {
        "sender_batch_header": {
            "sender_batch_id":batch_id,
            "email_subject": "Test from agent",
            "email_message": "A test payout"
        },
        "items": paypal_items
    }

    #--------SENDING ENGINE-------------#
    payout_request = PayoutsPostRequest()
    payout_request.request_body(body)

    try:
        response = client.execute(request=payout_request)
        print("Payout request submitted!")
        print("HTTP status:", response.status_code)

        result = response.result
        print("Batch ID:", result.batch_header.payout_batch_id)
        print("Batch status:", result.batch_header.batch_status)

        final_ans = {
            "msg":"payout request submitted!",
            "HTTP_status": response.status_code,
            "Batch_ID": result.batch_header.payout_batch_id,
            "Batch_status": result.batch_header.batch_status
        }

        return {
            "status": "success",
            "responses": final_ans,
            "follow_up_recipients": follow_up_recipients_from_items(items),
        } 

    except Exception as e:
        notification = send_sender_failure_followup(str(e))
        return {
            "status": "error",
            "message": f"Payout request failed: {e}",
            "sender_follow_up_email": notification,
        }


     
     



def greet_user(name: str) -> dict:
    """Greet the user by name."""
    return {
        "status": "success",
        "message": f"Hello {name}, it is a pleasure to see you!",
    }


 

root_agent = Agent(
    model="gemini-3.6-flash",
    name="processing_agent",
    description="Converts uploaded Excel spreadsheets into structured PayPal payout items.",
    instruction=(
        "For an uploaded Excel spreadsheet, preserve the existing workflow: call "
        "read_excel_from_upload first. For a Google Sheets URL, call read_google_sheet first. "
        "For either source, inspect the tool result. If status is not 'success', do not call "
        "payout_folks; explain the error to the user. If status is 'success', take ONLY the "
        "value of its 'items' field (a list of payout-item dictionaries) and pass that list "
        "as the 'items' argument to payout_folks. Do not pass the entire result dictionary, "
        "do not recreate or modify the items, and do not ask the user to paste the items again. "
        "After payout_folks returns, report its returned status and PayPal batch details. "
        "This is a PayPal Sandbox integration.\n"
        "A successful submission only means PayPal accepted the batch; it does not mean "
        "every item succeeded. Preserve the returned follow_up_recipients metadata and, "
        "after a successful submission, make one best-effort call to track_payout_batch "
        "with the returned batch ID and that metadata. If items are still nonterminal, "
        "tell the user to track the batch again later. The tracking tool sends recipient follow-up emails for "
        "failed items and reports which emails were sent, skipped, or failed. For a sender-"
        "side submission error, clearly explain the error and whether the operator follow-up "
        "email was sent."
    ),
    tools=[read_excel_from_upload, read_google_sheet, payout_folks, track_payout_batch, greet_user],
)

 
