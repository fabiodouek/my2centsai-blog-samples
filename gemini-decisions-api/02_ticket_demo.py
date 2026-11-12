"""The demo: one support ticket, three question types, one request.

The answers drive a tiny triage rule at the end, which is the point of the API:
probabilities you can put thresholds on, instead of text you have to parse.
"""

import json

from common import MODEL, TICKET, banner, record_sdk_usage, save, sdk_client, usd

QUESTIONS = {
    "double_charge": {
        "type": "noul",
        "instructions": "Does `message` report being charged more than once for the same purchase?",
    },
    "asks_for_refund": {
        "type": "noul",
        "instructions": "Does the customer explicitly ask for money back?",
    },
    "mentions_legal_action": {
        "type": "noul",
        "instructions": "Does the customer threaten legal action or a chargeback?",
    },
    "queue": {
        "type": "choice",
        "instructions": "Which support queue should handle this ticket?",
        "criteria": {
            "technical": "App errors, crashes, login problems.",
            "billing": "Charges, refunds, invoices, payment methods.",
            "shipping": "Parcel tracking, delays, damaged or lost parcels.",
            "account": "Plan changes, users, permissions, cancellations.",
            "other": "Anything that fits none of the above.",
        },
    },
    "churn_risk": {
        "type": "score",
        "instructions": "How likely is the customer to leave if this is not resolved quickly?",
        "criteria": [
            "No churn signal",
            "Mild frustration",
            "Mentions alternatives or a deadline",
            "States they will leave",
        ],
    },
}

client = sdk_client()
result = client.decisions.create(model=MODEL, input=json.dumps(TICKET), questions=QUESTIONS)
record_sdk_usage(result)
a = result.answers

banner("yes/no (noul): probability of yes, clamped to 0.01..0.99")
for key in ("double_charge", "asks_for_refund", "mentions_legal_action"):
    print(f"{key:<24} P(yes)={a[key].noul:<6} confidence={a[key].confidence}")

banner("choice: I sent the options as technical, billing, shipping, account, other")
print(f"choice={a['queue'].choice}  confidence={a['queue'].confidence}")
print("keys in the order the API returned them:")
for option, p in a["queue"].probabilities.items():
    print(f"  {option:<10} {p}")

banner("score: probability-weighted average of the level index (0-based)")
print(f"score={a['churn_risk'].score}  confidence={a['churn_risk'].confidence}")
for level, p in a["churn_risk"].probabilities.items():
    print(f"  {level} {p:<6} {a['churn_risk'].legend[level]}")

banner("triage rule built on the answers")
if a["queue"].confidence < 0.6:
    route = "human_review"
elif a["double_charge"].noul > 0.9 and a["asks_for_refund"].noul > 0.9:
    route = f"{a['queue'].choice}/refund_fast_track"
else:
    route = a["queue"].choice
priority = "P1" if a["churn_risk"].score >= 2 or a["mentions_legal_action"].noul > 0.5 else "P3"
print(f"route={route} priority={priority}")

u = result.usage
banner("usage")
print(f"input tokens={u.total_input_tokens} output tokens={u.total_output_tokens} cost={usd(u.total_input_tokens)}")
save("02_ticket_demo", result.model_dump(exclude_none=True))
