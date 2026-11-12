"""The same first request through the google-genai SDK (3.2.0 or later)."""

from google import genai
from google.genai import errors

from common import MODEL, api_key, banner, record_sdk_usage, sdk_client

QUESTIONS = {"is_damaged": {"type": "noul", "instructions": "Did the item arrive damaged?"}}
INPUT = "The blue mug I ordered on Monday arrived cracked on Wednesday."

banner(f"google-genai {genai.__version__}, default client (API version v1)")
try:
    genai.Client(api_key=api_key()).decisions.create(model=MODEL, input=INPUT, questions=QUESTIONS)
    print("worked on the default API version")
except errors.APIError as e:
    print(f"{type(e).__name__}: {e.code} {str(e.message)[:200]}")
except Exception as e:  # show whatever the SDK raises, untouched
    print(f"{type(e).__name__}: {str(e)[:200]}")

banner("client pinned to v1beta")
client = sdk_client()
result = client.decisions.create(model=MODEL, input=INPUT, questions=QUESTIONS)
record_sdk_usage(result)
answer = result.answers["is_damaged"]
print(f"P(yes)     = {answer.noul}")
print(f"confidence = {answer.confidence}")
print(f"usage      = {result.usage.model_dump(exclude_none=True)}")
