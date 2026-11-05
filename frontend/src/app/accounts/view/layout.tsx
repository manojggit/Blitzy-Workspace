// Route layout of /accounts/view, the CAVW screen: program COACTVWC, BMS map COACTVW. The page is
// rendered on demand, so its server payload records the query of the request it answers. A
// prerendered payload records an empty query whatever the URL carried, and the App Router would
// then file the ?acctId entry of a direct load under the bare path, so a later main menu entry
// would fetch that account instead of showing the empty key screen.
import type { ReactNode } from "react";

// Next.js reads the segment config only under the name dynamic, so it stays camelCase as a
// framework-mandated exception to the MK_Modernization Rule's upper-case constants.
export const dynamic = "force-dynamic";

export default function AccountViewLayout({ children }: Readonly<{ children: ReactNode }>) {
    return children;
}
