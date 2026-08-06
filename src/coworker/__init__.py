from __future__ import annotations

import os

# mem0 reads this setting at import time and otherwise enables PostHog telemetry.
# Establish Coworker's privacy default before any submodule can import mem0 while
# preserving an explicit process-level opt-in.
os.environ.setdefault("MEM0_TELEMETRY", "false")
