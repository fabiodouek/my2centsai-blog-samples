"""Delete every sandbox the server lists, past the first page. Run it after a crashed script.

Needs the port-forward (see common.py). Prints "deleted N". To delete only the sandboxes of one
run, call delete_all_sandboxes(metadata={"run": ...}) from common.py instead.
"""

import sys

from common import delete_all_sandboxes, require_server

require_server()  # one line and exit 1 if the server is down or rejects the API key
try:
    print(f"deleted {delete_all_sandboxes()}")
except Exception as error:  # the server went away mid-run, or the SDK or httpx gave up: one line, not a traceback
    sys.exit(f"could not delete the sandboxes: {type(error).__name__}: {str(error)[:200]}")
