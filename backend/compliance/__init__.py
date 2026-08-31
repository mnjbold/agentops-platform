"""Phase C compliance package (DNC + time-of-day).

Two independent helpers live here:

* :mod:`compliance.dnc` — DNC list lookup + 30-day cache.
* :mod:`compliance.time_window` — phone-number → IANA timezone + the
  TCPA-style 8am-9pm local-time window check.

Both are pure-Python (no I/O at import time, no FastAPI dependencies) so
the test suite can exercise them without spinning up the API.
"""
