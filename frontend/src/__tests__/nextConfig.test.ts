// Tests: next.config.ts. next build and next dev refuse a NEXT_PUBLIC_API_URL that is not the
// backend origin, naming the variable; every other phase loads without it; and a valid origin
// keeps the standalone output and the / -> /signon entry redirect (CC00, COSGN00C).
import {
    PHASE_DEVELOPMENT_SERVER,
    PHASE_PRODUCTION_BUILD,
    PHASE_PRODUCTION_SERVER,
    PHASE_TEST,
} from "next/constants";

import BuildNextConfig from "../../next.config";

const INLINING_PHASES = [PHASE_PRODUCTION_BUILD, PHASE_DEVELOPMENT_SERVER];
const UNCHECKED_PHASES = [PHASE_TEST, PHASE_PRODUCTION_SERVER];
const BACKEND_ORIGIN = "http://127.0.0.1:8000";
// Assembled at run time, so no credential-shaped literal sits in the source.
const CREDENTIAL_ORIGIN = `http://${["user", "pw"].join(":")}@127.0.0.1:8000`;
const INVALID_API_URLS: (string | undefined)[] = [
    undefined,
    "",
    "   ",
    ` ${BACKEND_ORIGIN}`,
    "127.0.0.1:8000",
    "ftp://127.0.0.1:8000",
    CREDENTIAL_ORIGIN,
    `${BACKEND_ORIGIN}/api/v1`,
    `${BACKEND_ORIGIN}?x=1`,
    `${BACKEND_ORIGIN}#x`,
    // URL parsing turns each of these into the clean origin; the API client sends the raw value.
    "http:127.0.0.1:8000",
    "http:/127.0.0.1:8000",
    "http:\\\\127.0.0.1:8000",
    `${BACKEND_ORIGIN}\\`,
    "http://127.0.0.1:80\t00",
];
const VALID_API_URLS = [
    BACKEND_ORIGIN,
    "http://127.0.0.1:20000/",
    "https://api.example.com",
    "http://[::1]:8000",
];
// The quoted label keeps blank and whitespace-only values distinct in the test titles.
const INVALID_CASES = INLINING_PHASES.flatMap((phase) =>
    INVALID_API_URLS.map((apiUrl) => ({ phase, apiUrl, label: JSON.stringify(apiUrl) ?? "unset" })),
);
const VALID_CASES = INLINING_PHASES.flatMap((phase) =>
    VALID_API_URLS.map((apiUrl) => ({ phase, apiUrl })),
);
// The message names the variable first, then where to set it.
const API_URL_ERROR = /^NEXT_PUBLIC_API_URL .*frontend\/\.env\.local/;
const ENTRY_REDIRECT = { source: "/", destination: "/signon", permanent: false };

let savedApiUrl: string | undefined;

function SetApiUrl(apiUrl: string | undefined): void {
    if (apiUrl === undefined) {
        delete process.env.NEXT_PUBLIC_API_URL;
    } else {
        process.env.NEXT_PUBLIC_API_URL = apiUrl;
    }
}

beforeEach(() => {
    savedApiUrl = process.env.NEXT_PUBLIC_API_URL;
});

afterEach(() => {
    SetApiUrl(savedApiUrl);
});

it.each(INVALID_CASES)("$phase refuses NEXT_PUBLIC_API_URL $label", ({ phase, apiUrl }) => {
    SetApiUrl(apiUrl);
    expect(() => BuildNextConfig(phase)).toThrow(API_URL_ERROR);
});

it.each(VALID_CASES)(
    "$phase accepts the backend origin $apiUrl and keeps the config",
    async ({ phase, apiUrl }) => {
        SetApiUrl(apiUrl);
        const config = BuildNextConfig(phase);
        expect(config.output).toBe("standalone");
        expect(config.reactStrictMode).toBe(true);
        expect(config.poweredByHeader).toBe(false);
        await expect(config.redirects?.()).resolves.toStrictEqual([ENTRY_REDIRECT]);
    },
);

it.each(UNCHECKED_PHASES)("%s loads the config with NEXT_PUBLIC_API_URL unset", (phase) => {
    SetApiUrl(undefined);
    expect(BuildNextConfig(phase).output).toBe("standalone");
});
