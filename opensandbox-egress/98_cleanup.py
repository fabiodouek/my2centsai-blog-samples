"""Delete the sandboxes these scripts created (metadata example=opensandbox-egress). Prints `deleted N`."""

from common import cleanup, ensure_port_forward, require_server

ensure_port_forward()
require_server()
print(f"deleted {cleanup()}")
