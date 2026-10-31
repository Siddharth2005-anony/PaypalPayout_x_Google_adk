from google.adk.agents.llm_agent import Agent
from google.adk.models import LiteLlm

import os, uuid
from io import BytesIO

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



chap = LiteLlm(
    model="openai/gpt-6-luna",
    api_key=os.getenv("OPENAI_API_KEY")
)


def natural_language_processing(prompt: str) -> dict:
    """ Extract details from the user's prompt and transform it to the paypal item list.
    For example: USER: "pay jone 5.65$, his paypal email id is jone@example.com"
    transform that prompt to paypal payout oriented items

    - if anything is missing from user side, make sure user is explicit and
    ask them and fetch all necessary things like valid paypal email and amount. (USD is default) 
    
    - finally transfer the results to payout folks. 
    """

    api_key = os.getenv("OPENAI_API_KEY")

    sys_prompt = f"""
    your task is to transform the given prompt to paypal payout items.
    For example: USER: "pay jone 5.65$, his paypal email id is jone@example.com"
        transform that prompt to paypal payout oriented items
    
        - if anything is missing from user side, 
        ask them and fetch all necessary things like valid paypal email and amount. (USD is default) 
        - if the user gives a separate Gmail or follow-up address, preserve it as
        follow_up_email for failure notifications.
    """

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.parse(
                    model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
                    input=[
                        {
                            "role": "system",
                            "content": (
                                sys_prompt
                            ),
                        },
                        {"role": "user", "content": prompt},
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

        validation_error = validate_payout_items(
            [item.model_dump() for item in batch.items]
        )
        if validation_error:
            return {
                "status": "error",
                "message": f"Please provide explicit payout details: {validation_error}",
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



instructions = f"""
You are a helpful agent, who uses relevant tools like natural_language_processing, payout_folks
and transform them to paypal payout items and pay them. always follow the order:
- 1st extract useful info from user query and toss the results to payout_folks.
- 2nd use payout_folks and results from natural_language_processing and pay them
- If a PayPal email or amount is missing or ambiguous, stop and ask the user for
  the exact recipient email and amount before paying.
- A submitted batch is not proof that every item succeeded. When the user asks to track
- A submitted batch is not proof that every item succeeded. After a successful submission,
  make one best-effort call to track_payout_batch with the batch ID and the returned
  follow_up_recipients. Clearly report PayPal failure reasons and email notification results;
  if items are still pending, tell the user to track the batch again later.
"""

root_agent = Agent(
    model=chap,
    name='root_agent',
    description='A helpful assistant for paying out only from user queries.',
    instruction= instructions,
    tools=[natural_language_processing, payout_folks, track_payout_batch]
)
