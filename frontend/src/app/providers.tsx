"use client";
// Replaces: COMMAREA identity (COCOM01Y) and screen attributes; installs the M3 theme
// (AAP 0.5, 0.7.1) for every BMS-map page.
import type { ReactNode } from "react";
import { ThemeProvider } from "@mui/material/styles";
import { CssBaseline, GlobalStyles } from "@mui/material";

import { AUTOFILL_KEYFRAMES_STYLE, CARD_DEMO_THEME } from "@/app/theme";
import { SessionProvider } from "@/lib/session";

export interface ProvidersProps {
    children: ReactNode;
}

// CssBaseline sits inside the theme so the M3 surface colour and type scale reach <body>;
// SessionProvider sits inside it too, so anything the session context renders is themed.
// GlobalStyles inserts the input autofill keyframes once per app: the theme stops every MUI input
// inserting its own copy, which the App Router Emotion cache would record on each mount for good.
export function Providers({ children }: ProvidersProps) {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CssBaseline />
            <GlobalStyles styles={AUTOFILL_KEYFRAMES_STYLE} />
            <SessionProvider>{children}</SessionProvider>
        </ThemeProvider>
    );
}
