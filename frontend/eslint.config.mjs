// Frontend lint gate: enforces the MK_Modernization Rule (naming, with upper-case literal
// constants; 100-character lines; braces; function size; parameter count; no swallowed errors; no
// raw HTML injection) and the design-system rule that screens use MUI components instead of raw
// HTML controls and headings.
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import eslintConfigPrettier from "eslint-config-prettier/flat";

const MAX_FUNCTION_LINES = 20;
const MAX_PARAMS = 4;
// Prettier's printWidth. Prettier leaves an over-long string, comment or test title on one line,
// so the gate also measures every physical line against this limit.
const MAX_LINE_LENGTH = 100;

const SCRIPT_FILES = ["**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}"];
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

const UPPER_SNAKE_CASE_PATTERN = /^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$/;
const TYPE_ASSERTION_NODES = ["TSAsExpression", "TSSatisfiesExpression"];
// Next.js reads these route-segment and metadata exports only under their camelCase names.
const NEXT_FILE_CONVENTION_EXPORTS = [
    "metadata",
    "viewport",
    "dynamic",
    "dynamicParams",
    "revalidate",
    "fetchCache",
    "runtime",
    "preferredRegion",
    "maxDuration",
];

// Fixed literal data: a string, number, bigint, boolean, null or regex literal, a negated number,
// a template without substitutions, or a non-empty array or plain object built only from those.
// An empty [] or {} is an accumulator, not a constant.
const FIXED_LITERAL_CHECKS = {
    Literal: () => true,
    UnaryExpression: (node) => node.operator === "-" && IsNumericLiteral(node.argument),
    TemplateLiteral: (node) => node.expressions.length === 0,
    ArrayExpression: (node) => node.elements.length > 0 && node.elements.every(IsFixedElement),
    ObjectExpression: (node) =>
        node.properties.length > 0 && node.properties.every(IsFixedProperty),
};

function UnwrapTypeAssertions(node) {
    return TYPE_ASSERTION_NODES.includes(node.type) ? UnwrapTypeAssertions(node.expression) : node;
}

function IsNumericLiteral(node) {
    return node.type === "Literal" && ["number", "bigint"].includes(typeof node.value);
}

function IsFixedElement(element) {
    return element !== null && IsFixedLiteral(element);
}

function IsFixedProperty(property) {
    const plainKey = property.type === "Property" && !property.computed && !property.method;
    return plainKey && property.kind === "init" && IsFixedLiteral(property.value);
}

function IsFixedLiteral(node) {
    const value = UnwrapTypeAssertions(node);
    const check = FIXED_LITERAL_CHECKS[value.type];
    return Object.hasOwn(FIXED_LITERAL_CHECKS, value.type) && check(value);
}

function IsFrameworkExport(declarator) {
    const exported = declarator.parent.parent.type === "ExportNamedDeclaration";
    return exported && NEXT_FILE_CONVENTION_EXPORTS.includes(declarator.id.name);
}

function ReportLowerCaseLiteralConstant(context, declarator) {
    const { id, init } = declarator;
    if (id.type !== "Identifier" || init === null || UPPER_SNAKE_CASE_PATTERN.test(id.name)) {
        return;
    }
    if (IsFixedLiteral(init) && !IsFrameworkExport(declarator)) {
        context.report({ node: id, messageId: "upperCaseConstant", data: { name: id.name } });
    }
}

function CreateLiteralConstantCaseVisitor(context) {
    return {
        "VariableDeclaration[kind='const'] > VariableDeclarator": (declarator) =>
            ReportLowerCaseLiteralConstant(context, declarator),
    };
}

function ReportLongLines(context) {
    context.sourceCode.lines.forEach((line, index) => {
        const length = [...line].length;
        if (length > MAX_LINE_LENGTH) {
            const loc = { line: index + 1, column: MAX_LINE_LENGTH };
            context.report({
                loc,
                messageId: "lineTooLong",
                data: { length, limit: MAX_LINE_LENGTH },
            });
        }
    });
}

function CreateLineLengthVisitor(context) {
    return { Program: () => ReportLongLines(context) };
}

const LITERAL_CONSTANT_CASE_RULE = {
    meta: {
        type: "suggestion",
        schema: [],
        messages: { upperCaseConstant: "Literal constant '{{name}}' must be UPPER_SNAKE_CASE." },
    },
    create: CreateLiteralConstantCaseVisitor,
};

const MAX_LINE_LENGTH_RULE = {
    meta: {
        type: "layout",
        schema: [],
        messages: { lineTooLong: "Line is {{length}} characters; the limit is {{limit}}." },
    },
    create: CreateLineLengthVisitor,
};

// Local rules for checks no installed rule makes: core max-len is deprecated, and
// naming-convention cannot tell a literal const from a computed one by its initializer.
const MODERNIZATION_PLUGIN = {
    rules: {
        "max-line-length": MAX_LINE_LENGTH_RULE,
        "literal-constant-case": LITERAL_CONSTANT_CASE_RULE,
    },
};

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
        files: SCRIPT_FILES,
        plugins: { modernization: MODERNIZATION_PLUGIN },
        rules: {
            "@typescript-eslint/naming-convention": ["error", ...NAMING_CONVENTION_OPTIONS],
            "modernization/literal-constant-case": "error",
            "modernization/max-line-length": "error",
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
