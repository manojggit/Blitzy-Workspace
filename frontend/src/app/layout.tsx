// Root layout for the CardDemo pages replacing the BMS mapsets (AAP 0.5, 0.6.3);
// title per COTTL01Y.
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Roboto } from "next/font/google";
import { AppRouterCacheProvider } from "@mui/material-nextjs/v16-appRouter";

import { Providers } from "@/app/providers";

// next/font requires the loader call as a module-scope constant. The variable name is the one
// the theme's font stack reads, so the self-hosted face reaches every MUI typography role.
const roboto = Roboto({
    weight: ["300", "400", "500", "700"],
    subsets: ["latin"],
    display: "swap",
    variable: "--font-roboto",
});

// Next.js reads this export only under the name metadata, so it stays camelCase as a
// framework-mandated exception to the MK_Modernization Rule's upper-case constants. The tab
// title is CCDA-TITLE02, the application name every legacy screen header shows.
export const metadata: Metadata = {
    title: "CardDemo",
    icons: { icon: "/favicon.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
    return (
        <html lang="en" className={roboto.variable}>
            <body>
                <AppRouterCacheProvider>
                    <Providers>{children}</Providers>
                </AppRouterCacheProvider>
            </body>
        </html>
    );
}
