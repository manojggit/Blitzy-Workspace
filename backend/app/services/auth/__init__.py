"""CC00 sign-on services. Replaces: COSGN00C (CC00), BMS COSGN00.

    cosgn00c: the COSGN00C port: sign-on edits, USRSEC lookup, password verification,
        admin/regular routing and F3 sign-off.
    login_throttle: the sign-on lockout guarding cosgn00c; it has no legacy source.

Import submodules by full path, for example: from app.services.auth import cosgn00c
"""
