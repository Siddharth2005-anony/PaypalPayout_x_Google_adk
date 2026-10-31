import os
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

 

class Amount(BaseModel):
    value: str
    currency: str

class PayPalItem(BaseModel):
    recipient_type: str
    receiver: str
    amount: Amount
    note: str
    sender_item_id: str

class PayoutBatch(BaseModel):
    items: list[PayPalItem]

def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    client = OpenAI(api_key=api_key)
    df = pd.read_excel("Testing_batch_1.xlsx")
    response = client.responses.parse(
        model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
        input=[
            {
                "role": "system",
                "content": (
                    "Convert spreadsheet rows into PayPal payout items. "
                    "Copy values exactly; never change amounts or emails."
                ),
            },
            {"role": "user", "content": df.to_csv(index=False)},
        ],
        text_format=PayoutBatch,
    )
    batch = response.output_parsed
    if batch is None:
        raise RuntimeError("The payout conversion returned no structured result.")

    for i, item in enumerate(batch.items, start=1):
        item.amount.currency = "USD"
        item.sender_item_id = f"fork_{i}"

    print(batch.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
