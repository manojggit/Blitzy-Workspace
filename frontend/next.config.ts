// Next.js configuration for the CardDemo frontend: standalone output, the sign-on entry
// redirect, and a build-time check that NEXT_PUBLIC_API_URL is the backend origin. Replaces CICS
// transaction CC00 (program COSGN00C, map COSGN00) as the application entry point.
import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER, PHASE_PRODUCTION_BUILD } from "next/constants";

const API_URL_VARIABLE = "NEXT_PUBLIC_API_URL";
// next build and next dev inline NEXT_PUBLIC_* values into the browser bundle, so a missing or
// malformed origin would otherwise ship silently. next/jest loads this file in phase-test, and
// the standalone server never loads it, so no other phase is checked.
const INLINING_PHASES: readonly string[] = [PHASE_PRODUCTION_BUILD, PHASE_DEVELOPMENT_SERVER];
const ALLOWED_PROTOCOLS: readonly string[] = ["http:", "https:"];
// src/lib/apiClient.ts drops trailing slashes before appending /api/v1, so "/" and "//" pass.
const ORIGIN_PATH_PATTERN = /^\/+$/;
// The API client concatenates the raw value, but URL parsing turns http:host, http:/host,
// backslashes and inner tabs or newlines into a clean origin, and axios refuses a value with no
// "//" after the scheme. So the written form must also be the scheme, "//", an authority with
// no "/", "\", whitespace, "?", "#" or "@", and nothing after it but slashes.
const RAW_ORIGIN_PATTERN = /^https?:\/\/[^/\\\s?#@]+\/*$/i;
const API_URL_GUIDANCE =
    "Set it to the backend origin, scheme://host[:port] with no path " +
    "(e.g. http://127.0.0.1:8000), in frontend/.env.local (copy frontend/.env.local.example), " +
    "in the environment, or as the Docker build argument. Next.js inlines it into the browser " +
    "bundle at build time, so rebuild after changing it.";

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

function DescribeOriginProblem(parsedUrl: URL, apiUrl: string): string | null {
    if (!ALLOWED_PROTOCOLS.includes(parsedUrl.protocol)) {
        return `uses ${parsedUrl.protocol} instead of http: or https:`;
    }
    if (parsedUrl.username !== "" || parsedUrl.password !== "") {
        return "carries a user name or password";
    }
    if (!ORIGIN_PATH_PATTERN.test(parsedUrl.pathname)) {
        return `carries the path ${parsedUrl.pathname}; the API client appends /api/v1 itself`;
    }
    if (!RAW_ORIGIN_PATTERN.test(apiUrl)) {
        return "is not written as http(s)://host[:port]";
    }
    return null;
}

// The API client concatenates the raw value, so even an empty "?" or "#" breaks every call.
function DescribeApiUrlProblem(apiUrl: string | undefined): string | null {
    if (apiUrl === undefined) {
        return "is not set";
    }
    if (apiUrl.trim() === "") {
        return "is blank";
    }
    if (apiUrl.trim() !== apiUrl) {
        return "has leading or trailing whitespace";
    }
    if (apiUrl.includes("?") || apiUrl.includes("#")) {
        return "carries a query string or fragment";
    }
    // URL.canParse tests the value without catching the TypeError that new URL throws.
    if (!URL.canParse(apiUrl)) {
        return "is not an absolute URL";
    }
    return DescribeOriginProblem(new URL(apiUrl), apiUrl);
}

function AssertBackendOrigin(apiUrl: string | undefined): void {
    const problem = DescribeApiUrlProblem(apiUrl);
    if (problem !== null) {
        throw new Error(`${API_URL_VARIABLE} ${problem}. ${API_URL_GUIDANCE}`);
    }
}

// Next.js calls a function export with the phase it loads the configuration for.
export default function BuildNextConfig(phase: string): NextConfig {
    if (INLINING_PHASES.includes(phase)) {
        AssertBackendOrigin(process.env[API_URL_VARIABLE]);
    }
    return nextConfig;
}
