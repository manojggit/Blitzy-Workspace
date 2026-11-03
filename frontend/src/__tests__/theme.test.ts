// Tests for: frontend/src/app/theme.ts (CARD_DEMO_THEME), the Material Design 3 token theme of
// AAP 0.5.3–0.5.4.
import type { Theme } from "@mui/material/styles";
import { CARD_DEMO_THEME } from "@/app/theme";

type ColourReader = (theme: Theme) => string | undefined;

interface ColourExpectation {
    token: string;
    read: ColourReader;
    expected: string;
}

type M3TypeRoleName =
    | "displayLarge"
    | "displayMedium"
    | "displaySmall"
    | "headlineLarge"
    | "headlineMedium"
    | "headlineSmall"
    | "titleLarge"
    | "titleMedium"
    | "titleSmall"
    | "bodyLarge"
    | "bodyMedium"
    | "bodySmall"
    | "labelLarge"
    | "labelMedium"
    | "labelSmall";

interface TypeRoleExpectation {
    variant: M3TypeRoleName;
    fontSize: number;
    lineHeight: number;
    fontWeight: number;
}

interface StyleCallbackProps {
    theme: Theme;
    ownerState?: object;
}

type StyleCallback = (styleProps: StyleCallbackProps) => unknown;

interface ButtonVariantEntry {
    props: unknown;
    style: unknown;
}

interface ButtonThemeEntry {
    variants?: ButtonVariantEntry[];
    styleOverrides?: { root?: unknown };
}

type StyleRecord = Record<string, unknown>;

interface SegmentStateExpectation {
    state: string;
    selectorPath: readonly string[];
    expectedStyle: StyleRecord;
}

const PRIMARY_CONTAINER_HEX = "#EADDFF";
const ON_PRIMARY_CONTAINER_HEX = "#4F378B";

const M3_COLOR_EXPECTATIONS: readonly ColourExpectation[] = [
    { token: "primary", read: (theme) => theme.palette.primary.main, expected: "#6750A4" },
    {
        token: "onPrimary",
        read: (theme) => theme.palette.primary.contrastText,
        expected: "#FFFFFF",
    },
    {
        token: "primaryContainer",
        read: (theme) => theme.palette.m3.primaryContainer,
        expected: PRIMARY_CONTAINER_HEX,
    },
    {
        token: "onPrimaryContainer",
        read: (theme) => theme.palette.m3.onPrimaryContainer,
        expected: ON_PRIMARY_CONTAINER_HEX,
    },
    { token: "secondary", read: (theme) => theme.palette.secondary.main, expected: "#625B71" },
    { token: "tertiary", read: (theme) => theme.palette.m3.tertiary, expected: "#7D5260" },
    { token: "error", read: (theme) => theme.palette.error.main, expected: "#B3261E" },
    {
        token: "errorContainer",
        read: (theme) => theme.palette.m3.errorContainer,
        expected: "#F9DEDC",
    },
    { token: "surface", read: (theme) => theme.palette.background.default, expected: "#FEF7FF" },
    {
        token: "surfaceContainer",
        read: (theme) => theme.palette.background.paper,
        expected: "#F3EDF7",
    },
    { token: "onSurface", read: (theme) => theme.palette.text.primary, expected: "#1D1B20" },
    {
        token: "onSurfaceVariant",
        read: (theme) => theme.palette.text.secondary,
        expected: "#49454F",
    },
    { token: "outline", read: (theme) => theme.palette.m3.outline, expected: "#79747E" },
    { token: "outlineVariant", read: (theme) => theme.palette.divider, expected: "#CAC4D0" },
];

const M3_SHAPE_SCALE_EXPECTED = { extraSmall: 4, small: 8, medium: 12, large: 16, extraLarge: 28 };
const MEDIUM_CORNER_RADIUS = 12;

const SPACING_UNITS = 4;
const SPACING_PIXELS = "16px";

const ROOT_FONT_SIZE_PIXELS = 16;
// Absorbs only floating-point error from rem or unitless-ratio conversion, so pixel drift fails.
const PIXEL_PRECISION_DIGITS = 5;

const ROBOTO_FONT_STACK = "var(--font-roboto, Roboto), Roboto, Helvetica, Arial, sans-serif";

const M3_TYPE_EXPECTATIONS: readonly TypeRoleExpectation[] = [
    { variant: "displayLarge", fontSize: 57, lineHeight: 64, fontWeight: 400 },
    { variant: "displayMedium", fontSize: 45, lineHeight: 52, fontWeight: 400 },
    { variant: "displaySmall", fontSize: 36, lineHeight: 44, fontWeight: 400 },
    { variant: "headlineLarge", fontSize: 32, lineHeight: 40, fontWeight: 400 },
    { variant: "headlineMedium", fontSize: 28, lineHeight: 36, fontWeight: 400 },
    { variant: "headlineSmall", fontSize: 24, lineHeight: 32, fontWeight: 400 },
    { variant: "titleLarge", fontSize: 22, lineHeight: 28, fontWeight: 400 },
    { variant: "titleMedium", fontSize: 16, lineHeight: 24, fontWeight: 500 },
    { variant: "titleSmall", fontSize: 14, lineHeight: 20, fontWeight: 500 },
    { variant: "bodyLarge", fontSize: 16, lineHeight: 24, fontWeight: 400 },
    { variant: "bodyMedium", fontSize: 14, lineHeight: 20, fontWeight: 400 },
    { variant: "bodySmall", fontSize: 12, lineHeight: 16, fontWeight: 400 },
    { variant: "labelLarge", fontSize: 14, lineHeight: 20, fontWeight: 500 },
    { variant: "labelMedium", fontSize: 12, lineHeight: 16, fontWeight: 500 },
    { variant: "labelSmall", fontSize: 11, lineHeight: 16, fontWeight: 500 },
];

const TONAL_VARIANT_NAME = "tonal";
const OUTLINED_VARIANT_NAME = "outlined";

const SELECTED_SELECTOR = "&.Mui-selected";
const DISABLED_SELECTOR = "&.Mui-disabled";
const HOVER_SELECTOR = "&:hover";

const SELECTED_ENABLED_SEGMENT_STYLE = {
    backgroundColor: CARD_DEMO_THEME.palette.m3.primaryContainer,
    color: CARD_DEMO_THEME.palette.m3.onPrimaryContainer,
};

const SELECTED_DISABLED_SEGMENT_STYLE = {
    backgroundColor: CARD_DEMO_THEME.palette.action.disabledBackground,
    backgroundImage: "none",
    color: CARD_DEMO_THEME.palette.action.disabled,
};

const SEGMENT_STATE_EXPECTATIONS: readonly SegmentStateExpectation[] = [
    {
        state: "selected enabled",
        selectorPath: [SELECTED_SELECTOR],
        expectedStyle: SELECTED_ENABLED_SEGMENT_STYLE,
    },
    {
        state: "selected disabled",
        selectorPath: [SELECTED_SELECTOR, DISABLED_SELECTOR],
        expectedStyle: SELECTED_DISABLED_SEGMENT_STYLE,
    },
    {
        state: "hovered selected disabled",
        selectorPath: [SELECTED_SELECTOR, DISABLED_SELECTOR, HOVER_SELECTOR],
        expectedStyle: SELECTED_DISABLED_SEGMENT_STYLE,
    },
];

function FontSizeToPixels(value: string | number | undefined): number {
    if (typeof value === "number") {
        return value;
    }
    if (typeof value === "string" && value.endsWith("rem")) {
        return Number.parseFloat(value) * ROOT_FONT_SIZE_PIXELS;
    }
    if (typeof value === "string" && value.endsWith("px")) {
        return Number.parseFloat(value);
    }
    return Number.NaN;
}

// A unitless line height is a CSS ratio of the font size.
function LineHeightToPixels(value: string | number | undefined, fontSizePixels: number): number {
    if (typeof value === "number") {
        return value * fontSizePixels;
    }
    if (typeof value !== "string") {
        return Number.NaN;
    }
    if (value.endsWith("px") || value.endsWith("rem")) {
        return FontSizeToPixels(value);
    }
    return Number(value) * fontSizePixels;
}

// createTheme keeps the case the author wrote, so colours compare case-insensitively.
function NormaliseColour(value: string | undefined): string {
    return (value ?? "").toUpperCase();
}

function AsColourString(value: unknown): string | undefined {
    return typeof value === "string" ? value : undefined;
}

function ResolveStyle(style: unknown, styleProps: StyleCallbackProps): unknown {
    if (typeof style === "function") {
        return (style as StyleCallback)(styleProps);
    }
    return style;
}

function ReadRootVariants(root: unknown): readonly ButtonVariantEntry[] {
    const resolvedRoot = ResolveStyle(root, { theme: CARD_DEMO_THEME, ownerState: {} });
    if (typeof resolvedRoot !== "object" || resolvedRoot === null) {
        return [];
    }
    if (!("variants" in resolvedRoot) || !Array.isArray(resolvedRoot.variants)) {
        return [];
    }
    return resolvedRoot.variants as ButtonVariantEntry[];
}

// Read without MUI's typing, which marks components.MuiButton.variants deprecated.
function CollectButtonVariants(): readonly ButtonVariantEntry[] {
    const buttonEntry = CARD_DEMO_THEME.components?.MuiButton as unknown as
        ButtonThemeEntry | undefined;
    const componentVariants = buttonEntry?.variants ?? [];
    return [...componentVariants, ...ReadRootVariants(buttonEntry?.styleOverrides?.root)];
}

function IsVariantProps(props: unknown, variantName: string): boolean {
    if (typeof props !== "object" || props === null || !("variant" in props)) {
        return false;
    }
    return props.variant === variantName;
}

function FindVariantStyle(variantName: string): Record<string, unknown> | undefined {
    const matchingEntry = CollectButtonVariants().find((entry) =>
        IsVariantProps(entry.props, variantName),
    );
    if (matchingEntry === undefined) {
        return undefined;
    }
    const resolvedStyle = ResolveStyle(matchingEntry.style, { theme: CARD_DEMO_THEME });
    if (typeof resolvedStyle !== "object" || resolvedStyle === null) {
        return undefined;
    }
    return resolvedStyle as Record<string, unknown>;
}

function AsStyleRecord(value: unknown): StyleRecord | undefined {
    if (typeof value !== "object" || value === null) {
        return undefined;
    }
    return value as StyleRecord;
}

// Walks the resolved ToggleButton root override down a chain of nested selectors.
function ReadSegmentRule(selectorPath: readonly string[]): StyleRecord | undefined {
    const segmentRoot = CARD_DEMO_THEME.components?.MuiToggleButton?.styleOverrides?.root;
    const resolvedRoot = AsStyleRecord(ResolveStyle(segmentRoot, { theme: CARD_DEMO_THEME }));
    return selectorPath.reduce<StyleRecord | undefined>(
        (rule, selector) => AsStyleRecord(rule?.[selector]),
        resolvedRoot,
    );
}

describe("M3 colour roles (AAP 0.5.3)", () => {
    it.each(M3_COLOR_EXPECTATIONS)("$token is $expected", ({ read, expected }) => {
        expect(NormaliseColour(read(CARD_DEMO_THEME))).toBe(expected.toUpperCase());
    });
});

describe("M3 shape and spacing (AAP 0.5.3)", () => {
    it(`shape.borderRadius is the ${MEDIUM_CORNER_RADIUS} px M3 medium corner`, () => {
        expect(CARD_DEMO_THEME.shape.borderRadius).toBe(MEDIUM_CORNER_RADIUS);
    });

    it("shape.m3 holds the 4 / 8 / 12 / 16 / 28 px M3 corner scale", () => {
        expect(CARD_DEMO_THEME.shape.m3).toEqual(M3_SHAPE_SCALE_EXPECTED);
    });

    it(`spacing(${SPACING_UNITS}) is ${SPACING_PIXELS} on the 4 dp grid`, () => {
        expect(CARD_DEMO_THEME.spacing(SPACING_UNITS)).toBe(SPACING_PIXELS);
    });
});

describe("M3 type scale (AAP 0.5.3, gap 0.5.4)", () => {
    it("typography.fontFamily is the Roboto stack", () => {
        expect(CARD_DEMO_THEME.typography.fontFamily).toBe(ROBOTO_FONT_STACK);
    });

    it.each(M3_TYPE_EXPECTATIONS)(
        "$variant is Roboto $fontSize px on a $lineHeight px line at weight $fontWeight",
        ({ variant, fontSize, lineHeight, fontWeight }) => {
            const roleStyle = CARD_DEMO_THEME.typography[variant];
            expect(roleStyle).toBeDefined();
            const measuredFontSize = FontSizeToPixels(roleStyle.fontSize);
            const measuredLineHeight = LineHeightToPixels(roleStyle.lineHeight, measuredFontSize);
            expect(measuredFontSize).toBeCloseTo(fontSize, PIXEL_PRECISION_DIGITS);
            expect(measuredLineHeight).toBeCloseTo(lineHeight, PIXEL_PRECISION_DIGITS);
            expect(roleStyle.fontWeight).toBe(fontWeight);
            expect(roleStyle.fontFamily).toBe(ROBOTO_FONT_STACK);
        },
    );
});

describe("Material Design 3 gap resolutions (AAP 0.5.4)", () => {
    it("the tonal Button variant uses the primaryContainer tokens", () => {
        const tonalStyle = FindVariantStyle(TONAL_VARIANT_NAME);
        expect(tonalStyle).toBeDefined();
        const fillColour = AsColourString(tonalStyle?.backgroundColor ?? tonalStyle?.background);
        const themeContainer = NormaliseColour(CARD_DEMO_THEME.palette.m3.primaryContainer);
        expect(NormaliseColour(fillColour)).toBe(themeContainer);
        expect(themeContainer).toBe(PRIMARY_CONTAINER_HEX);
        expect(NormaliseColour(AsColourString(tonalStyle?.color))).toBe(ON_PRIMARY_CONTAINER_HEX);
    });

    it("TextField defaults to the outlined variant", () => {
        const textFieldDefaults = CARD_DEMO_THEME.components?.MuiTextField?.defaultProps;
        expect(textFieldDefaults?.variant).toBe(OUTLINED_VARIANT_NAME);
    });
});

describe("M3 segmented-button states (gap 0.5.4)", () => {
    it.each(SEGMENT_STATE_EXPECTATIONS)(
        "a $state segment uses its theme tokens",
        ({ selectorPath, expectedStyle }) => {
            expect(ReadSegmentRule(selectorPath)).toMatchObject(expectedStyle);
        },
    );
});
