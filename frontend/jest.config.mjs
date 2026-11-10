// Jest configuration for the CardDemo frontend unit and screen suites (src/__tests__).
// next/jest supplies the SWC transform, the CSS, image and next/font mocks, and loads
// next.config.ts and the .env files before the options below are merged in.
import nextJest from "next/jest.js";

const createJestConfig = nextJest({ dir: "./" });

const JEST_CONFIG = {
    testEnvironment: "jsdom",
    setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
    // The "@/*" -> "src/*" alias of tsconfig.json.
    moduleNameMapper: {
        "^@/(.*)$": "<rootDir>/src/$1",
    },
    testMatch: ["<rootDir>/src/__tests__/**/*.test.ts", "<rootDir>/src/__tests__/**/*.test.tsx"],
    // The Playwright specs in e2e/ run only through npm run test:e2e.
    testPathIgnorePatterns: ["<rootDir>/node_modules/", "<rootDir>/.next/", "<rootDir>/e2e/"],
    // Keeps the Next.js build output, including .next/standalone/package.json, out of Jest's
    // module map, where its package name would collide with <rootDir>/package.json.
    modulePathIgnorePatterns: ["<rootDir>/.next/"],
    // The screen suites key every field through user-event, so on a contended host a test can
    // take several times its idle duration without hanging; 30 s still catches a real hang.
    testTimeout: 30000,
    clearMocks: true,
    moduleFileExtensions: ["ts", "tsx", "js", "mjs", "json"],
};

export default createJestConfig(JEST_CONFIG);
