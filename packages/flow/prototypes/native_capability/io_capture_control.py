"""Owned fixed I/O only; no runtime/provider import or native API."""

import sys

sys.stdout.buffer.write(b"openadapt-owned-stdout/v1\n")
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b"openadapt-owned-stderr/v1\n")
sys.stderr.buffer.flush()
