// Route layout of /admin/users/delete, the CU03 screen: program COUSR03C, BMS map COUSR03. The
// page is rendered on demand, so its server payload records the query of the request it answers.
// A prerendered payload records an empty query whatever the URL carried, and the App Router would
// then file the ?userId entry of a user list selection or a direct load under the bare path, so a
// later admin menu entry would fetch that user instead of showing the empty key screen.
import type { ReactNode } from "react";

// Next.js reads the segment config only under the name dynamic, so it stays camelCase as a
// framework-mandated exception to the MK_Modernization Rule's upper-case constants.
export const dynamic = "force-dynamic";

export default function AdminUsersDeleteLayout({ children }: Readonly<{ children: ReactNode }>) {
    return children;
}
