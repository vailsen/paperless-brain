# config/extraction_rules/en.py
"""English extraction profile — common international document types.

Deliberately smaller than the German profile: it covers the document types most
Paperless-ngx installs actually use, rather than guessing at jurisdiction-specific
ones. Anything not listed here falls through to `_default`, which still produces
usable extraction — it just lacks type-specific guidance.

Keys must match the `document_type` names in YOUR Paperless instance. Rename or
extend them to fit; see the "Extraction rules" section of the README.
"""

from config.extraction_rules.base import BASE_INSTRUCTIONS

RULES: dict[str, dict] = {
    # === INVOICES & PAYMENTS ===
    "Invoice": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Invoice.
Pay particular attention to:
- Invoice number, invoice date and service period
- Total amount, net/tax breakdown, tax rate
- Payment due date and payment terms (discount, instalments)
- Payee and bank details / payment reference
- Customer, contract or order number
""",
    },
    "Receipt": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Receipt / proof of purchase.
Pay particular attention to:
- Merchant, date and time of purchase
- Individual line items with prices, total amount, tax
- Payment method
- Any warranty or return period stated on the receipt
""",
    },
    "Bank Statement": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Bank or credit card statement.
Pay particular attention to:
- Account holder, account/IBAN and statement period
- Opening and closing balance
- Individual transactions as a table: date, description, amount
- Fees, interest and any overdraft information
""",
    },
    "Payslip": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Payslip / salary statement.
Pay particular attention to:
- Employer, employee and pay period
- Gross pay, net pay and the deduction breakdown (tax, social contributions)
- Year-to-date totals
- Changes compared with the previous period
""",
    },
    # === CONTRACTS & AGREEMENTS ===
    "Contract": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Contract / agreement.
Pay particular attention to:
- Contracting parties and contract number
- Start date, term, and renewal mechanism
- Notice period and cancellation deadline — record these as actions with a deadline
- Recurring charges and payment schedule
- Any clause that requires the holder to act by a certain date
""",
    },
    "Rental Agreement": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Rental / lease agreement.
Pay particular attention to:
- Landlord, tenant and the property address
- Rent, service charges and deposit
- Start of tenancy, minimum term and notice period
- Obligations regarding repairs, subletting and renovation
""",
    },
    "Insurance Policy": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Insurance policy.
Pay particular attention to:
- Insurer, policy number and insured person/object
- Scope of cover, sum insured and excess/deductible
- Premium, payment interval and due date
- Policy term, renewal date and cancellation deadline
""",
    },
    # === OFFICIAL & TAX ===
    "Tax Assessment": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Tax assessment / tax notice.
Pay particular attention to:
- Issuing authority, tax reference number and assessment period
- Amount payable or refund due, and the calculation basis
- Payment deadline — record it as an action with a deadline
- Appeal/objection period and how to file
""",
    },
    "Notice": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Official notice / decision from an authority.
Pay particular attention to:
- Issuing authority and reference number
- What has been decided, and on what grounds
- Any deadline to act, object or appeal — record it as an action with a deadline
- Consequences of missing the deadline
""",
    },
    "Certificate": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Certificate / official confirmation.
Pay particular attention to:
- Issuing body and the person or thing certified
- What exactly is confirmed, and the period it covers
- Date of issue and any expiry date
- Reference or registration numbers
""",
    },
    # === CORRESPONDENCE & REPORTS ===
    "Letter": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Letter / correspondence.
Pay particular attention to:
- Sender, recipient and date
- The purpose of the letter and any request made of the recipient
- Deadlines and required responses — record them as actions
- Referenced contract, case or invoice numbers
""",
    },
    "Report": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Report / expert opinion.
Pay particular attention to:
- Author, commissioning party and date
- Subject of the assessment and the method used
- Findings, conclusions and any recommended actions
- Measured values and tabular data — capture these as tables
""",
    },
    # === FALLBACK ===
    "_default": {
        "prompt": BASE_INSTRUCTIONS
        + """
The document type is not pre-classified. Determine it from the content and
record it in the "document_type" field.
In general, pay attention to:
- Sender, recipient, date
- Any deadlines and required actions
- Contract and reference numbers
- Financial amounts and payment terms
""",
    },
}
