# PayPal Payout Agent

A Google ADK-style agent project for preparing PayPal payout batches from Excel files, public Google Sheets, or natural-language payout requests. Payouts are submitted to the PayPal Sandbox, then a separate tracking flow can inspect item-level results and send SMTP follow-up emails for failed items.

## DEMO URL: https://youtu.be/DV8VMhcQAik 

## What it does

- Converts spreadsheet rows into structured PayPal payout items using an OpenAI structured response.
- Accepts a public Google Sheets URL and exports the selected tab as CSV.
- Converts a natural-language request into payout items after checking for a recipient email and amount.
- Submits batches through the PayPal Payouts SDK using Sandbox credentials.
- Tracks PayPal-generated payout batch IDs, including paginated item results.
- Reports successful, failed, and nonterminal payout items.
- Sends optional SMTP follow-up emails when a failed item has a separate notification address.
- Keeps follow-up email metadata local. It is removed before payout items are sent to PayPal.

## Project structure

```text
.
├── batch_tracking/
│   ├── agent.py          # PayPal batch lookup and failure reporting
│   └── __init__.py
├── natural_language/
│   ├── agent.py          # Natural-language payout extraction and submission
│   └── __init__.py
├── process_agent/
│   ├── agent.py          # Excel/Google Sheets processing and submission
│   ├── examp.py          # Small standalone Excel-to-structured-output example
│   ├── Testing_batch_1.xlsx
│   └── __init__.py
├── email_followup.py     # SMTP helpers and payout-item validation
├── .env.example          # Environment variable template
└── agent.md              # Repository task instructions
```

## Prerequisites

- Python 3.10 or newer.
- A Google ADK-compatible environment.
- An OpenAI API key for structured payout extraction and LiteLLM-backed agents.
- PayPal Sandbox client credentials with Payouts API access.
- SMTP credentials if failure follow-up emails are required.

The repository includes a `requirements.txt` with the libraries imported by the agents. Install it in a virtual environment:

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
```

The requirements are intentionally unpinned so they can follow the Google ADK and PayPal SDK versions selected by the deployment environment. If your environment uses a different distribution name for the imported `paypalpayoutssdk` package, install the corresponding PayPal Payouts SDK package instead.

## Configuration

Create a local `.env` file from the template:

```bash
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env` instead. Set the values below before making API calls:

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI key used for structured payout extraction. |
| `OPENAI_MODEL` | OpenAI model used by the extraction calls. Set this to a model available to your account. |
| `PAYPAL_CLIENT_ID` | PayPal Sandbox application client ID. |
| `PAYPAL_CLIENT_SECRET` | PayPal Sandbox application secret. |
| `SMTP_HOST` | SMTP server hostname. Defaults to `smtp.gmail.com`. |
| `SMTP_PORT` | SMTP port. Defaults to `587`. |
| `SMTP_USERNAME` | SMTP login username. |
| `SMTP_PASSWORD` | SMTP password or app password. |
| `SMTP_FROM_EMAIL` | Optional sender address. Defaults to the SMTP username. |
| `FOLLOW_UP_USER_EMAIL` | Optional operator address for sender-side submission failures. |

Never commit `.env`, API keys, PayPal secrets, SMTP passwords, or real recipient data. `.gitignore` keeps local environment files out of Git while allowing `.env.example` to be committed.

## Running with Google ADK

The packages expose `root_agent` objects for ADK discovery. From the repository root, use the ADK command available in your installation:

```bash
adk web
```

For a direct package run, the package names are:

```bash
adk run process_agent
adk run natural_language
adk run batch_tracking
```

The exact CLI flags can vary by Google ADK version. `process_agent` is the spreadsheet-oriented workflow, `natural_language` handles prompt-based payouts, and `batch_tracking` only reads existing PayPal Sandbox batches and reports their statuses.

## Input expectations

### Excel or Google Sheets

The spreadsheet workflow expects rows containing a PayPal recipient email and amount. A separate column named `gmail`, `gmail_address`, `follow_up_email`, or `notification_email` may be used for failure notifications. The sample workbook contains four test rows and uses `business.example.com` addresses, so it is suitable as a structural example rather than a real payout list.

Google Sheets links must be public to anyone with the link, use the `https://docs.google.com/spreadsheets/...` format, and contain no more than 1,000 rows. The CSV export is limited to 5 MB.

### Natural language

Include an exact PayPal email address and amount for every payout. If either is missing or ambiguous, the agent should stop before submission and request clarification. USD is enforced by the Python code for generated payout items.

## PayPal and notification behavior

Payout submission uses the PayPal Sandbox endpoint through the PayPal Payouts SDK. A successful submission means PayPal accepted the batch; it does not mean every item has completed successfully. Use the PayPal-generated `payout_batch_id` for tracking, not the locally generated sender batch ID.

After tracking, failed items are summarized with reasons returned by PayPal. SMTP notifications are attempted only when a valid follow-up address is available and SMTP is configured. Missing SMTP configuration is reported as skipped, not as a successful email.

## Validation

The current repository has no automated test suite. A basic syntax check is:

```bash
python -m compileall -q .
```

End-to-end validation requires working OpenAI, PayPal Sandbox, and optional SMTP credentials. Use Sandbox credentials and test recipient data while developing.

## Safety notes

- This project is configured for PayPal Sandbox flows, but it still handles payment-related credentials and recipient data.
- Review the extracted payout items before submission in any non-test environment.
- Do not pass the entire tool result into `payout_folks`; pass only its successful `items` list.
- Do not treat a submitted batch as proof that all payouts succeeded. Track the PayPal-generated batch ID.
