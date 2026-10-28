"""Build the template once: python template/build.py"""

import time

from dotenv import load_dotenv
from e2b import Template, default_build_logger

from template import template

load_dotenv()

if __name__ == "__main__":
    start = time.perf_counter()
    Template.build(
        template,
        "my2cents-claude-creds",
        cpu_count=2,
        memory_mb=2048,
        on_build_logs=default_build_logger(),
    )
    print(f"build took {time.perf_counter() - start:.0f}s")
