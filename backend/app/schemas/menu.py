"""Request and response models of the CardDemo main and admin menus.

Replaces: COMEN01C (CM00) and COADM01C (CA00), BMS maps COMEN01 (COMEN1A) and COADM01
(COADM1A), symbolic maps cpy-bms/COMEN01.CPY and cpy-bms/COADM01.CPY, and the option tables
of copybooks COMEN02Y (11 main-menu options) and COADM02Y (6 admin-menu options).

Both menus share these models. ``GET /menu/main`` and ``GET /menu/admin`` answer
``MenuResponse``; ``POST /menu/main/selection`` and ``POST /menu/admin/selection`` take
``MenuSelectionRequest`` and answer ``MenuSelectionResponse``. The selected option arrives as
raw OPTION text, so the services run the programs' own numeric and range edits on it.
"""

from pydantic import BaseModel, Field

from app.schemas.common import ScreenText

# BMS LENGTH of the OPTION field in both COMEN01 and COADM01.
OPTION_FIELD_LENGTH = 2
# PIC X(35) of CDEMO-MENU-OPT-NAME and CDEMO-ADMIN-OPT-NAME.
OPTION_LABEL_LENGTH = 35


class MenuOption(BaseModel):
    """One entry of a menu table, as COMEN01C and COADM01C list it on COMEN01 and COADM01.

    Replaces the ``NN. <name>`` text BUILD-MENU-OPTIONS moves into OPTN001O-OPTN012O. The
    frontend shows the label as the item text and the number as its secondary text.
    """

    number: int = Field(
        ge=1,
        le=99,
        description=(
            "The option number (CDEMO-MENU-OPT-NUM 1-11, or CDEMO-ADMIN-OPT-NUM 1-6), "
            "which the user posts to select the option."
        ),
    )
    label: str = Field(
        max_length=OPTION_LABEL_LENGTH,
        description=(
            "The option name verbatim from the menu table (CDEMO-MENU-OPT-NAME or "
            "CDEMO-ADMIN-OPT-NAME), right-trimmed, for example 'Transaction Add'."
        ),
    )


class MenuResponse(BaseModel):
    """The option list of the main menu (COMEN01, COMEN01C) or the admin menu (COADM01, COADM01C).

    It holds every option of the table, including main-menu option 11 and admin options 5-6,
    whose programs are not part of this application and answer "not installed" when selected.
    """

    options: list[MenuOption] = Field(
        description=(
            "Every option of the COMEN02Y or COADM02Y table, in table order (option 1 first)."
        ),
    )


class MenuSelectionRequest(BaseModel):
    """The ENTER key on COMEN01 (COMEN01C) or COADM01 (COADM01C): the keyed option number.

    The text is sent as keyed. COMEN01C and COADM01C zero-fill it and reject a non-numeric,
    zero or out-of-range value with ``Please enter a valid option number...``.
    """

    option: ScreenText(OPTION_FIELD_LENGTH) = Field(
        default="",
        description=(
            "The OPTION map field (BMS LENGTH 2), raw text; blank and non-numeric values "
            "reach the program's own option edit."
        ),
    )


class MenuSelectionResponse(BaseModel):
    """The outcome of a valid selection on COMEN01 (COMEN01C) or COADM01 (COADM01C).

    A selection either navigates, replacing ``EXEC CICS XCTL`` to the option's program, or
    stays on the menu with the program's message literal. An invalid option or a role refusal
    is an error body instead, not this model.
    """

    route: str | None = Field(
        default=None,
        description=(
            "The frontend path the option opens, for example '/accounts/view' or "
            "'/admin/users', replacing XCTL to the option's program; null when the menu stays."
        ),
    )
    message: str = Field(
        default="",
        description=(
            "The program's ERRMSG literal when no navigation happens, verbatim: "
            "'This option <name> is not installed...' (COMEN01C), "
            "'This option is not installed ...' (COADM01C) or "
            "'This option <name>is coming soon ...'; empty when the option navigates."
        ),
    )
