# Test 6: what reaches Cloud Logging. Runs ON the instance (./run.sh t6_logging.sh).
# A canary value goes into a sandbox through --env; afterwards we search for it in Cloud Logging
# (see t6_search.sh), once with gcloud and once in Logs Explorer.
SB=/usr/local/gcp/bin/sandbox
$SB do --env TOKEN=m2c-canary-7731 -- /bin/echo hello
