// Tests: the route layouts of every page that reads a context query parameter:
// app/accounts/view/layout.tsx (CAVW, program COACTVWC, BMS map COACTVW),
// app/accounts/update/layout.tsx (CAUP, program COACTUPC, BMS map COACTUP),
// app/cards/view/layout.tsx (CCDL, program COCRDSLC, BMS map COCRDSL),
// app/cards/update/layout.tsx (CCUP, program COCRDUPC, BMS map COCRDUP),
// app/transactions/view/layout.tsx (CT01, program COTRN01C, BMS map COTRN01),
// app/admin/users/update/layout.tsx (CU02, program COUSR02C, BMS map COUSR02) and
// app/admin/users/delete/layout.tsx (CU03, program COUSR03C, BMS map COUSR03). Each layout
// renders its route on demand, so the App Router keys every route entry by its own query and a
// menu entry with no query never reopens a list selection or a direct load's key; each passes its
// page through unchanged.
import { render, screen } from "@testing-library/react";
import { readdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";
import type { ReactElement, ReactNode } from "react";

import AccountUpdateLayout, {
    dynamic as accountUpdateRendering,
} from "@/app/accounts/update/layout";
import AccountViewLayout, { dynamic as accountViewRendering } from "@/app/accounts/view/layout";
import AdminUsersDeleteLayout, {
    dynamic as userDeleteRendering,
} from "@/app/admin/users/delete/layout";
import AdminUsersUpdateLayout, {
    dynamic as userUpdateRendering,
} from "@/app/admin/users/update/layout";
import CardUpdateLayout, { dynamic as cardUpdateRendering } from "@/app/cards/update/layout";
import CardViewLayout, { dynamic as cardViewRendering } from "@/app/cards/view/layout";
import TransactionViewLayout, {
    dynamic as transactionViewRendering,
} from "@/app/transactions/view/layout";

interface QueryRouteLayoutCase {
    route: string;
    RouteLayout: (props: Readonly<{ children: ReactNode }>) => ReactNode;
    rendering: string;
}

// The Next.js segment config value that renders a route for every request, never prerendered.
const ON_DEMAND_RENDERING = "force-dynamic";
const PAGE_TEST_ID = "query-route-page";
const PAGE_TEXT = "query route page";
const APP_DIRECTORY = resolve(__dirname, "..", "..", "app");
const PAGE_FILE_NAME = "page.tsx";
const QUERY_READ_PATTERN = /\buseSearchParams\(/;

const QUERY_ROUTE_LAYOUT_CASES: readonly QueryRouteLayoutCase[] = [
    { route: "/accounts/view", RouteLayout: AccountViewLayout, rendering: accountViewRendering },
    {
        route: "/accounts/update",
        RouteLayout: AccountUpdateLayout,
        rendering: accountUpdateRendering,
    },
    { route: "/cards/view", RouteLayout: CardViewLayout, rendering: cardViewRendering },
    { route: "/cards/update", RouteLayout: CardUpdateLayout, rendering: cardUpdateRendering },
    {
        route: "/transactions/view",
        RouteLayout: TransactionViewLayout,
        rendering: transactionViewRendering,
    },
    {
        route: "/admin/users/update",
        RouteLayout: AdminUsersUpdateLayout,
        rendering: userUpdateRendering,
    },
    {
        route: "/admin/users/delete",
        RouteLayout: AdminUsersDeleteLayout,
        rendering: userDeleteRendering,
    },
];

// What the App Router hands a layout as its children: the route's page.
function PageStandIn(): ReactElement {
    return <span data-testid={PAGE_TEST_ID}>{PAGE_TEXT}</span>;
}

function ReadsQuery(pageEntry: string): boolean {
    return QUERY_READ_PATTERN.test(readFileSync(join(APP_DIRECTORY, pageEntry), "utf8"));
}

// The route of every page.tsx under src/app whose source reads its query with useSearchParams.
function QueryReadingRoutes(): string[] {
    const entries = readdirSync(APP_DIRECTORY, { recursive: true, encoding: "utf8" });
    return entries
        .filter((entry) => basename(entry) === PAGE_FILE_NAME && ReadsQuery(entry))
        .map((entry) => `/${dirname(entry).split(sep).join("/")}`)
        .sort();
}

it("gives every page that reads its query one of these on-demand route layouts", () => {
    const layoutRoutes = QUERY_ROUTE_LAYOUT_CASES.map((layoutCase) => layoutCase.route).sort();
    expect(QueryReadingRoutes()).toEqual(layoutRoutes);
});

it.each(QUERY_ROUTE_LAYOUT_CASES)(
    "$route layout exports the segment config dynamic as force-dynamic",
    ({ rendering }) => {
        expect(rendering).toBe(ON_DEMAND_RENDERING);
    },
);

it.each(QUERY_ROUTE_LAYOUT_CASES)(
    "$route layout renders its page as its only content, unwrapped",
    ({ RouteLayout }) => {
        const { container } = render(
            <RouteLayout>
                <PageStandIn />
            </RouteLayout>,
        );
        const page = screen.getByTestId(PAGE_TEST_ID);
        expect(container.childNodes).toHaveLength(1);
        expect(container.firstChild).toBe(page);
        expect(page).toHaveTextContent(PAGE_TEXT);
    },
);
