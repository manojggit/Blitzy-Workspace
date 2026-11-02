// Next.js configuration for the CardDemo frontend: standalone output and the sign-on entry
// redirect. Replaces CICS transaction CC00 (program COSGN00C, map COSGN00) as the application
// entry point.
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
    output: "standalone",
    reactStrictMode: true,
    poweredByHeader: false,
    // frontend/ is the only npm project in the repository. Pinning both roots here keeps
    // server.js directly under .next/standalone/ and stops Next.js inferring a parent folder
    // as the workspace root.
    outputFileTracingRoot: __dirname,
    turbopack: {
        root: __dirname,
    },
    // Next.js looks this method up by name, so it keeps camelCase as a framework-mandated
    // exception to the MK_Modernization Rule's PascalCase method naming.
    async redirects() {
        // Temporary, so browsers do not cache the entry route.
        return [{ source: "/", destination: "/signon", permanent: false }];
    },
};

export default nextConfig;
