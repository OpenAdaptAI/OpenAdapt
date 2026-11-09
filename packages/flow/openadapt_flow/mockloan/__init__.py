"""MockLoan: a static, hash-routed fake loan-origination SPA demo target.

The NON-healthcare companion to :mod:`openadapt_flow.mockmed`. Its consequential
write is authorizing a disbursement of funds to a borrower's loan - an
irreversible money-movement write, the lending analog of MockMed's clinical
"Save Encounter". All data is fake. The app is deterministic (no
animations/transitions) and supports UI-drift modes via ``?drift=`` and
transactional fault injection via ``?fault=`` (see ``fault_server``).
"""

from openadapt_flow.mockloan.server import serve  # noqa: F401

__all__ = ["serve"]
