"""Test environment defaults.

Must run before application modules are imported so that provider lookups see
a deterministic environment.
"""

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEEPSEEK_API_KEY", "unit-test-key")
