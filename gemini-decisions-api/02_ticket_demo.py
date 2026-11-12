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
    print(f"{key:<24} P(yes)={a[key].noul:.4f}  confidence={a[key].confidence:.4f}")

banner("choice: probabilities over the five queues")
print(f"choice={a['queue'].choice}  confidence={a['queue'].confidence:.4f}")
for option, p in sorted(a["queue"].probabilities.items(), key=lambda kv: -kv[1]):
    print(f"  {option:<10} {p:.4f}")
print(f"  sum        {sum(a['queue'].probabilities.values()):.4f}")

banner("score: probability-weighted average of the level index (0-based)")
print(f"score={a['churn_risk'].score:.4f}  confidence={a['churn_risk'].confidence:.4f}")
for level, p in sorted(a["churn_risk"].probabilities.items()):
    print(f"  {level}  {p:.4f}  {a['churn_risk'].legend[level]}")

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
