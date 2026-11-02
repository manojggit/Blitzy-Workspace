// Frontend lint gate: enforces the MK_Modernization Rule (naming, braces, function size,
// parameter count, no swallowed errors, no raw HTML injection) and the design-system rule that
// screens use MUI components instead of raw HTML controls and headings.
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import eslintConfigPrettier from "eslint-config-prettier/flat";

const MAX_FUNCTION_LINES = 20;
const MAX_PARAMS = 4;

const SCRIPT_FILES = ["**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}"];
const TYPESCRIPT_FILES = ["**/*.{ts,tsx,mts,cts}"];
// eslint-config-next registers the react plugin for these files only (not *.cjs); a react rule
// enabled on any other file fails config validation with "Could not find plugin".
const REACT_PLUGIN_FILES = ["**/*.{js,jsx,mjs,ts,tsx,mts,cts}"];

const FORBIDDEN_RAW_ELEMENTS = [
    { element: "button", message: "use the MUI Button component." },
    { element: "input", message: "use the MUI TextField component." },
    { element: "select", message: "use MUI Select or a TextField with select." },
    { element: "table", message: "use the MUI Table components." },
    { element: "h1", message: "use the MUI Typography component." },
    { element: "h2", message: "use the MUI Typography component." },
    { element: "h3", message: "use the MUI Typography component." },
    { element: "h4", message: "use the MUI Typography component." },
    { element: "h5", message: "use the MUI Typography component." },
    { element: "h6", message: "use the MUI Typography component." },
];

// Function names the frameworks only recognise in camelCase: React hooks, and the Next.js
// file-convention exports (page/layout metadata, static params, proxy, instrumentation).
const FRAMEWORK_CAMEL_CASE_FUNCTIONS = [
    "use[A-Z].*",
    "generateMetadata",
    "generateViewport",
    "generateStaticParams",
    "generateImageMetadata",
    "generateSitemaps",
    "middleware",
    "proxy",
    "register",
    "onRequestError",
];
const FRAMEWORK_CAMEL_CASE_PATTERN = `^(${FRAMEWORK_CAMEL_CASE_FUNCTIONS.join("|")})$`;

// The naming rule treats a function-typed property (onPress: () => void) as a type method. Event
// handler props keep React's camelCase on[A-Z] form, which JSX and MUI props use.
const EVENT_HANDLER_PATTERN = "^on[A-Z]";

const NAMING_CONVENTION_OPTIONS = [
    { selector: "default", format: ["camelCase"], leadingUnderscore: "allow" },
    { selector: "import", format: ["camelCase", "PascalCase"] },
    { selector: "typeLike", format: ["PascalCase"] },
    { selector: "enumMember", format: ["UPPER_CASE"] },
    // The two filters are mutually exclusive, so selector precedence never decides the format.
    {
        selector: "function",
        filter: { regex: FRAMEWORK_CAMEL_CASE_PATTERN, match: false },
        format: ["PascalCase"],
    },
    {
        selector: "function",
        filter: { regex: FRAMEWORK_CAMEL_CASE_PATTERN, match: true },
        format: ["camelCase"],
    },
    // Components are function declarations, so a PascalCase arrow constant is reported.
    { selector: "variable", format: ["camelCase", "UPPER_CASE"] },
    // Destructuring keeps the snake_case JSON keys of the backend schemas (card_ref, screen_token).
    { selector: "variable", modifiers: ["destructured"], format: null },
    { selector: "parameter", modifiers: ["destructured"], format: null },
    { selector: "parameter", format: ["camelCase"], leadingUnderscore: "allow" },
    { selector: "classMethod", format: ["PascalCase"] },
    {
        selector: "typeMethod",
        filter: { regex: EVENT_HANDLER_PATTERN, match: false },
        format: ["PascalCase"],
    },
    {
        selector: "typeMethod",
        filter: { regex: EVENT_HANDLER_PATTERN, match: true },
        format: ["camelCase"],
    },
    // snake_case API keys, MUI theme keys (MuiButton) and camelCase framework keys.
    {
        selector: ["objectLiteralProperty", "typeProperty", "classProperty"],
        format: ["camelCase", "snake_case", "PascalCase", "UPPER_CASE"],
    },
    // Quoted keys such as HTTP headers ("Content-Type") and CSS selectors ("&:hover").
    {
        selector: ["objectLiteralProperty", "typeProperty", "objectLiteralMethod"],
        modifiers: ["requiresQuotes"],
        format: null,
    },
    // Framework-defined object methods, such as redirects() in next.config.ts.
    { selector: "objectLiteralMethod", format: null },
];

export default defineConfig([
    globalIgnores([
        ".next/**",
        "node_modules/**",
        "next-env.d.ts",
        "test-results/**",
        "playwright-report/**",
        "blob-report/**",
        "coverage/**",
        "out/**",
    ]),
    ...nextVitals,
    ...nextTs,
    {
        files: TYPESCRIPT_FILES,
        rules: {
            "@typescript-eslint/naming-convention": ["error", ...NAMING_CONVENTION_OPTIONS],
        },
    },
    {
        files: SCRIPT_FILES,
        rules: {
            "max-lines-per-function": [
                "error",
                { max: MAX_FUNCTION_LINES, skipBlankLines: true, skipComments: true, IIFEs: true },
            ],
            "max-params": ["error", MAX_PARAMS],
            "no-empty": "error",
        },
    },
    {
        files: REACT_PLUGIN_FILES,
        rules: {
            "react/no-danger": "error",
            "react/forbid-elements": ["error", { forbid: FORBIDDEN_RAW_ELEMENTS }],
        },
    },
    eslintConfigPrettier,
    // eslint-config-prettier switches curly off; "all" never conflicts with Prettier output.
    {
        files: SCRIPT_FILES,
        rules: {
            curly: ["error", "all"],
        },
    },
]);
