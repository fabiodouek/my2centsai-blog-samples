"""Call the gateway with an Entra ID token instead of the gateway key."""
import importlib

from azure.identity import DefaultAzureCredential

first = importlib.import_module("03_first_call")

if __name__ == "__main__":
    url, _ = first.gateway()
    cred = DefaultAzureCredential()
    for label, scope in [
        ("gateway audience", "https://ai.gateway.azure.com/.default"),
        ("ARM token (should be 401)", "https://management.azure.com/.default"),
    ]:
        token = cred.get_token(scope).token
        r, dt = first.call(url, token)
        print(f"{label}: {r.status_code} in {dt:.2f}s {r.text[:120]!r}")
