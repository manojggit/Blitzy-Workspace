// Tests: app/cards/view/layout.tsx (CCDL, program COCRDSLC, BMS map COCRDSL) and
// app/cards/update/layout.tsx (CCUP, program COCRDUPC, BMS map COCRDUP). Each layout renders its
// route on demand, so the App Router keys every route entry by its own query and a menu entry
// with no query never reopens a card list selection; each passes its page through unchanged.
import { render, screen } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";

import CardUpdateLayout, { dynamic as cardUpdateRendering } from "@/app/cards/update/layout";
import CardViewLayout, { dynamic as cardViewRendering } from "@/app/cards/view/layout";

interface CardRouteLayoutCase {
    route: string;
    RouteLayout: (props: Readonly<{ children: ReactNode }>) => ReactNode;
    rendering: string;
}

// The Next.js segment config value that renders a route for every request, never prerendered.
const ON_DEMAND_RENDERING = "force-dynamic";
const PAGE_TEST_ID = "card-route-page";
const PAGE_TEXT = "card route page";

const CARD_ROUTE_LAYOUT_CASES: readonly CardRouteLayoutCase[] = [
    { route: "/cards/view", RouteLayout: CardViewLayout, rendering: cardViewRendering },
    { route: "/cards/update", RouteLayout: CardUpdateLayout, rendering: cardUpdateRendering },
];

// What the App Router hands a layout as its children: the route's page.
function PageStandIn(): ReactElement {
    return <span data-testid={PAGE_TEST_ID}>{PAGE_TEXT}</span>;
}

it.each(CARD_ROUTE_LAYOUT_CASES)(
    "$route layout exports the segment config dynamic as force-dynamic",
    ({ rendering }) => {
        expect(rendering).toBe(ON_DEMAND_RENDERING);
    },
);

it.each(CARD_ROUTE_LAYOUT_CASES)(
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
