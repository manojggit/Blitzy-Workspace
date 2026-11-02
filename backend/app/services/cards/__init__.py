"""Card services: CCLI, CCDL and CCUP online programs ported 1:1.

cocrdslc  - COCRDSLC (CCDL), BMS COCRDSL: card detail view; shared key edits.
cocrdlic  - COCRDLIC (CCLI), BMS COCRDLI: 7-row card list, filters, paging, selection.
cocrdupc  - COCRDUPC (CCUP), BMS COCRDUP: card update state machine and single-unit write.

Consumers import each module by its full path (``from app.services.cards import cocrdlic``),
so this package re-exports nothing and importing it has no side effects.
"""
