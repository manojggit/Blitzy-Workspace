// Route layout of /cards/view, the CCDL screen: program COCRDSLC, BMS map COCRDSL. The page is
// rendered on demand, so its server payload records the query of the request it answers. A
// prerendered payload records an empty query whatever the URL carried, and the App Router would
// then file a card list selection's ?cardRef&acctId entry under the bare path, so a later menu
// entry would reopen the selected card instead of the empty key screen.
import type { ReactNode } from "react";

// Next.js reads the segment config only under the name dynamic, so it stays camelCase as a
// framework-mandated exception to the MK_Modernization Rule's upper-case constants.
export const dynamic = "force-dynamic";

export default function CardViewLayout({ children }: Readonly<{ children: ReactNode }>) {
    return children;
}
