"use client";
// Replaces: COMMAREA identity (COCOM01Y) and screen attributes; installs the M3 theme
// (AAP 0.5, 0.7.1) for every BMS-map page.
import type { ReactNode } from "react";
import { ThemeProvider } from "@mui/material/styles";
import { CssBaseline } from "@mui/material";

import { CARD_DEMO_THEME } from "@/app/theme";
import { SessionProvider } from "@/lib/session";

export interface ProvidersProps {
    children: ReactNode;
}

// CssBaseline sits inside the theme so the M3 surface colour and type scale reach <body>;
// SessionProvider sits inside it too, so anything the session context renders is themed.
export function Providers({ children }: ProvidersProps) {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <CssBaseline />
            <SessionProvider>{children}</SessionProvider>
        </ThemeProvider>
    );
}
