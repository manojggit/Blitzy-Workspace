"""COBOL-parity primitives shared by the CardDemo backend and batch.

Replaces: COBOL numeric semantics, CSUTLDTC/CSUTLDPY/CSUTLDWY date edits, CSLKPCDY lookups
and the copybook record layouts (CVACT01Y, CVCUS01Y, CVACT02Y, CVACT03Y, CVTRA01Y-07Y,
CSUSR01Y, COSTM01).

Consumers import each submodule directly, for example
``from app.utils.decimal_utils import StoreInto``; this package re-exports nothing.
"""
