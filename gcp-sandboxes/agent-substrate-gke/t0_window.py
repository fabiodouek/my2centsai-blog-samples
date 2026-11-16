"""Mark the start of the measured window. t10c_monitoring.py pulls Cloud Monitoring from here to now."""
import time
from common import save
save("window", {"start": time.time()})
print("window start recorded")
