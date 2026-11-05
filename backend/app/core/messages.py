"""Message literals shared by more than one ported CardDemo program.

Replaces: copybooks CSMSG01Y (common messages), CSMSG02Y (abend work area; the abend
literal is set in COACTUPC ABEND-ROUTINE) and COTTL01Y (screen titles), plus the paging
literals shared by COTRN00C and COUSR00C.

Values are the legacy literals character for character, with the COBOL PIC X(n) field
padding trimmed. Literals used by a single program stay in that program's service module.
"""

# CSMSG01Y CCDA-COMMON-MESSAGES.
THANK_YOU_MESSAGE = "Thank you for using CardDemo application..."
INVALID_KEY_MESSAGE = "Invalid key pressed. Please see below..."

# CSMSG02Y ABEND-MSG, as moved by COACTUPC ABEND-ROUTINE.
UNEXPECTED_ABEND_MESSAGE = "UNEXPECTED ABEND OCCURRED."

# COTTL01Y CCDA-SCREEN-TITLE.
TITLE_01 = "AWS Mainframe Modernization"
TITLE_02 = "CardDemo"

# Paging literals of COTRN00C and COUSR00C.
ALREADY_AT_TOP_MESSAGE = "You are already at the top of the page..."
ALREADY_AT_BOTTOM_MESSAGE = "You are already at the bottom of the page..."
AT_TOP_OF_PAGE_MESSAGE = "You are at the top of the page..."
REACHED_BOTTOM_MESSAGE = "You have reached the bottom of the page..."
REACHED_TOP_MESSAGE = "You have reached the top of the page..."

# COMEN01C admin-only refusal, also returned for role refusals. The trailing space is
# part of the legacy literal.
ADMIN_ONLY_MESSAGE = "No access - Admin Only option... "

# Sign-on lockout. The one literal with no legacy source: the login throttle has no
# counterpart in COSGN00C.
LOGIN_LOCKED_MESSAGE = "Too many failed attempts. Try again later ..."
