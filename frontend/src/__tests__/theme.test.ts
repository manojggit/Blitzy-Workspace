// Tests for: frontend/src/app/theme.ts (CARD_DEMO_THEME, VISUALLY_HIDDEN_STYLE,
// AUTOFILL_KEYFRAMES_STYLE, SCROLL_REGION_STYLE, FixedLayoutTableStyle, RowShareStyle,
// RowEndAlignmentFrom), the Material Design 3 token theme of AAP 0.5.3–0.5.4.
import type { Breakpoint, SxProps, Theme, TypeAction } from "@mui/material/styles";
import {
    AUTOFILL_KEYFRAMES_STYLE,
    CARD_DEMO_THEME,
    FixedLayoutTableStyle,
    RowEndAlignmentFrom,
    RowShareStyle,
    SCROLL_REGION_STYLE,
    VISUALLY_HIDDEN_STYLE,
} from "@/app/theme";

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
    letterSpacing: string;
}

type MuiTypographyVariantName =
    | "h1"
    | "h2"
    | "h3"
    | "h4"
    | "h5"
    | "h6"
    | "subtitle1"
    | "subtitle2"
    | "body1"
    | "body2"
    | "caption"
    | "overline"
    | "button";

interface BuiltInVariantExpectation {
    variant: MuiTypographyVariantName;
    role: M3TypeRoleName;
}

interface ComponentThemeEntry {
    defaultProps?: Record<string, unknown>;
    styleOverrides?: Record<string, unknown>;
}

interface ThemeRuleExpectation {
    rule: string;
    component: string;
    slot: string;
    selectorPath: readonly string[];
    expectedStyle: StyleRecord;
}

interface StyleVariantEntry {
    props: StyleRecord;
    style: StyleRecord;
}

interface SlotVariantExpectation {
    rule: string;
    component: string;
    slot: string;
    variantProps: StyleRecord;
    expectedStyle: StyleRecord;
}

interface ShadowExpectation {
    muiIndex: number;
    expectedShadow: string;
}

interface ActionRoleExpectation {
    key: keyof TypeAction;
    expected: string | number;
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

// The only colour roles the token table places under the augmented palette.m3 key, sorted.
const M3_AUGMENTED_COLOUR_ROLES = [
    "errorContainer",
    "onPrimaryContainer",
    "outline",
    "primaryContainer",
    "tertiary",
];
// Disabled field text and labels: on-surface at 38 %, as disabled buttons take it.
const ON_SURFACE_DISABLED_CONTENT = "rgba(29, 27, 32, 0.38)";

const M3_SHAPE_SCALE_EXPECTED = { extraSmall: 4, small: 8, medium: 12, large: 16, extraLarge: 28 };
const MEDIUM_CORNER_RADIUS = 12;

const SPACING_UNITS = 4;
const SPACING_PIXELS = "16px";

const ROOT_FONT_SIZE_PIXELS = 16;
// Absorbs only floating-point error from rem or unitless-ratio conversion, so pixel drift fails.
const PIXEL_PRECISION_DIGITS = 5;

const ROBOTO_FONT_STACK = "var(--font-roboto, Roboto), Roboto, Helvetica, Arial, sans-serif";

const M3_TYPE_EXPECTATIONS: readonly TypeRoleExpectation[] = [
    {
        variant: "displayLarge",
        fontSize: 57,
        lineHeight: 64,
        fontWeight: 400,
        letterSpacing: "-0.25px",
    },
    {
        variant: "displayMedium",
        fontSize: 45,
        lineHeight: 52,
        fontWeight: 400,
        letterSpacing: "0px",
    },
    {
        variant: "displaySmall",
        fontSize: 36,
        lineHeight: 44,
        fontWeight: 400,
        letterSpacing: "0px",
    },
    {
        variant: "headlineLarge",
        fontSize: 32,
        lineHeight: 40,
        fontWeight: 400,
        letterSpacing: "0px",
    },
    {
        variant: "headlineMedium",
        fontSize: 28,
        lineHeight: 36,
        fontWeight: 400,
        letterSpacing: "0px",
    },
    {
        variant: "headlineSmall",
        fontSize: 24,
        lineHeight: 32,
        fontWeight: 400,
        letterSpacing: "0px",
    },
    { variant: "titleLarge", fontSize: 22, lineHeight: 28, fontWeight: 400, letterSpacing: "0px" },
    {
        variant: "titleMedium",
        fontSize: 16,
        lineHeight: 24,
        fontWeight: 500,
        letterSpacing: "0.15px",
    },
    {
        variant: "titleSmall",
        fontSize: 14,
        lineHeight: 20,
        fontWeight: 500,
        letterSpacing: "0.1px",
    },
    { variant: "bodyLarge", fontSize: 16, lineHeight: 24, fontWeight: 400, letterSpacing: "0.5px" },
    {
        variant: "bodyMedium",
        fontSize: 14,
        lineHeight: 20,
        fontWeight: 400,
        letterSpacing: "0.25px",
    },
    { variant: "bodySmall", fontSize: 12, lineHeight: 16, fontWeight: 400, letterSpacing: "0.4px" },
    {
        variant: "labelLarge",
        fontSize: 14,
        lineHeight: 20,
        fontWeight: 500,
        letterSpacing: "0.1px",
    },
    {
        variant: "labelMedium",
        fontSize: 12,
        lineHeight: 16,
        fontWeight: 500,
        letterSpacing: "0.5px",
    },
    {
        variant: "labelSmall",
        fontSize: 11,
        lineHeight: 16,
        fontWeight: 500,
        letterSpacing: "0.5px",
    },
];

// The M3 role each built-in MUI typography variant must render.
const BUILT_IN_VARIANT_EXPECTATIONS: readonly BuiltInVariantExpectation[] = [
    { variant: "h1", role: "displayLarge" },
    { variant: "h2", role: "displayMedium" },
    { variant: "h3", role: "displaySmall" },
    { variant: "h4", role: "headlineMedium" },
    { variant: "h5", role: "headlineSmall" },
    { variant: "h6", role: "titleLarge" },
    { variant: "subtitle1", role: "titleMedium" },
    { variant: "subtitle2", role: "titleSmall" },
    { variant: "body1", role: "bodyLarge" },
    { variant: "body2", role: "bodyMedium" },
    { variant: "caption", role: "bodySmall" },
    { variant: "overline", role: "labelSmall" },
    { variant: "button", role: "labelLarge" },
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

const OUTLINE_HEX = "#79747E";
const ON_SURFACE_HOVER_LAYER = "rgba(29, 27, 32, 0.08)";
const ON_SURFACE_HOVER_GRADIENT =
    "linear-gradient(" + ON_SURFACE_HOVER_LAYER + ", " + ON_SURFACE_HOVER_LAYER + ")";
const M3_HOVER_OPACITY = 0.08;

// MUI's action colours on the M3 roles: on-surface-variant icons, the 10 % on-surface focus layer,
// 38 % disabled content and 12 % disabled containers. Selected and activated stay MUI's.
const ACTION_ROLE_EXPECTATIONS: readonly ActionRoleExpectation[] = [
    { key: "active", expected: "#49454F" },
    { key: "focus", expected: "rgba(29, 27, 32, 0.1)" },
    { key: "focusOpacity", expected: 0.1 },
    { key: "disabled", expected: "rgba(29, 27, 32, 0.38)" },
    { key: "disabledOpacity", expected: 0.38 },
    { key: "disabledBackground", expected: "rgba(29, 27, 32, 0.12)" },
    { key: "selected", expected: "rgba(0, 0, 0, 0.08)" },
    { key: "selectedOpacity", expected: 0.08 },
    { key: "activatedOpacity", expected: 0.12 },
];

const FOCUS_VISIBLE_SELECTOR = "&.Mui-focusVisible";
const FORCED_COLORS_QUERY = "@media (forced-colors: active)";
const ENABLED_SELECTED_SELECTOR = "&.Mui-selected:not(.Mui-disabled)";
const SINGLE_LINE_INPUT_SELECTOR = "&:not(textarea)";
const TOUCH_HOVER_RESTING_OUTLINE_SELECTOR =
    "&:hover:not(.Mui-focused):not(.Mui-error):not(.Mui-disabled) .MuiOutlinedInput-notchedOutline";
const SM_BREAKPOINT_QUERY = CARD_DEMO_THEME.breakpoints.up("sm");
const FORCED_SELECTED_OPTION_STYLE = {
    forcedColorAdjust: "none",
    backgroundColor: "Highlight",
    color: "HighlightText",
};

// The M3 outline, container and divider roles in place of MUI's Material Design 2 greys.
const OUTLINE_AND_FILL_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "a resting outlined field draws the outline role",
        component: "MuiOutlinedInput",
        slot: "notchedOutline",
        selectorPath: [],
        expectedStyle: { borderColor: OUTLINE_HEX },
    },
    {
        rule: "a touch hover keeps the outline role on a resting outlined field",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: ["@media (hover: none)", TOUCH_HOVER_RESTING_OUTLINE_SELECTOR],
        expectedStyle: { borderColor: OUTLINE_HEX },
    },
    {
        rule: "a resting outlined button draws the outline role",
        component: "MuiButton",
        slot: "outlined",
        selectorPath: [],
        expectedStyle: { "--variant-outlinedBorder": OUTLINE_HEX },
    },
    {
        rule: "a filled field sits on the 6 % on-surface container",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: [],
        expectedStyle: { backgroundColor: "rgba(29, 27, 32, 0.06)" },
    },
    {
        rule: "a hovered or focused filled field keeps the 6 % on-surface container",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: ["&:hover, &.Mui-focused"],
        expectedStyle: { backgroundColor: "rgba(29, 27, 32, 0.06)" },
    },
    {
        rule: "a pointer hover lays the 8 % on-surface state layer over a filled field",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: ["@media (hover: hover)", "&:hover:not(.Mui-disabled)"],
        expectedStyle: {
            backgroundImage: ON_SURFACE_HOVER_GRADIENT,
        },
    },
    {
        rule: "a disabled filled field takes the 4 % on-surface container",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: ["&.Mui-disabled"],
        expectedStyle: { backgroundColor: "rgba(29, 27, 32, 0.04)" },
    },
    {
        rule: "a filled field's active indicator is on-surface-variant",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: ["&::before"],
        expectedStyle: { borderBottomColor: "#49454F" },
    },
    {
        rule: "table rows are separated by the outline-variant divider",
        component: "MuiTableCell",
        slot: "root",
        selectorPath: [],
        expectedStyle: { borderBottomColor: "#CAC4D0" },
    },
    {
        rule: "an unselected segment label is on-surface",
        component: "MuiToggleButton",
        slot: "root",
        selectorPath: [],
        expectedStyle: { color: "#1D1B20" },
    },
];

// The M3 focus indicator: 3 px secondary, 2 px outside, or inside where an ancestor would clip it
// or a neighbour would sit under it.
const FOCUS_RING_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "every ButtonBase shows a 3 px secondary ring 2 px outside it",
        component: "MuiButtonBase",
        slot: "root",
        selectorPath: [FOCUS_VISIBLE_SELECTOR],
        expectedStyle: { outline: "3px solid #625B71", outlineOffset: 2 },
    },
    {
        rule: "a list item draws its ring inside itself",
        component: "MuiListItemButton",
        slot: "root",
        selectorPath: [FOCUS_VISIBLE_SELECTOR],
        expectedStyle: { outlineOffset: -3 },
    },
    {
        rule: "a menu item draws its ring inside itself",
        component: "MuiMenuItem",
        slot: "root",
        selectorPath: [FOCUS_VISIBLE_SELECTOR],
        expectedStyle: { outlineOffset: -3 },
    },
    {
        rule: "an icon button draws its ring inside itself",
        component: "MuiIconButton",
        slot: "root",
        selectorPath: [FOCUS_VISIBLE_SELECTOR],
        expectedStyle: { outlineOffset: -3 },
    },
    {
        rule: "a segment draws its ring inside itself, raised above the segment it overlaps",
        component: "MuiToggleButton",
        slot: "root",
        selectorPath: [FOCUS_VISIBLE_SELECTOR],
        expectedStyle: { outlineOffset: -3, zIndex: 1 },
    },
];

// M3 sizes: 40 dp buttons inside a 48 dp target, 48 dp menu items and icon buttons.
const TOUCH_TARGET_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "a button is at least 40 px tall with 8 px and 24 px padding",
        component: "MuiButton",
        slot: "root",
        selectorPath: [],
        expectedStyle: { minHeight: 40, padding: "8px 24px" },
    },
    {
        rule: "a button's pointer target reaches 4 px above and below it, never sideways",
        component: "MuiButton",
        slot: "root",
        selectorPath: ["&::after"],
        expectedStyle: {
            content: '""',
            position: "absolute",
            top: -4,
            bottom: -4,
            left: 0,
            right: 0,
        },
    },
    {
        rule: "an outlined button's pointer target reaches 4 px past its 1 px border",
        component: "MuiButton",
        slot: "outlined",
        selectorPath: ["&::after"],
        expectedStyle: { top: -5, bottom: -5 },
    },
    {
        rule: "a text button keeps 12 px beside its label",
        component: "MuiButton",
        slot: "text",
        selectorPath: [],
        expectedStyle: { padding: "8px 12px" },
    },
    {
        rule: "an outlined field input is a 24 px line box with 16 px above and below",
        component: "MuiOutlinedInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR],
        expectedStyle: {
            height: "24px",
            lineHeight: "24px",
            paddingTop: "16px",
            paddingBottom: "16px",
        },
    },
    {
        rule: "a menu item is 48 px tall below the sm breakpoint",
        component: "MuiMenuItem",
        slot: "root",
        selectorPath: [],
        expectedStyle: { minHeight: 48 },
    },
    {
        rule: "a menu item stays 48 px tall from the sm breakpoint",
        component: "MuiMenuItem",
        slot: "root",
        selectorPath: [SM_BREAKPOINT_QUERY],
        expectedStyle: { minHeight: 48 },
    },
    {
        rule: "a medium icon button pads its 24 px icon to 48 px",
        component: "MuiIconButton",
        slot: "sizeMedium",
        selectorPath: [],
        expectedStyle: { padding: "12px" },
    },
];

// Forced-colours mode: system-colour boundaries and the Highlight pair on the selected option.
const FORCED_COLOUR_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "a button draws a ButtonText boundary",
        component: "MuiButton",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY],
        expectedStyle: { border: "1px solid ButtonText" },
    },
    {
        rule: "a button's pointer target reaches 4 px past its 1 px boundary",
        component: "MuiButton",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY, "&::after"],
        expectedStyle: { top: -5, bottom: -5 },
    },
    {
        rule: "a disabled button draws a GrayText boundary",
        component: "MuiButton",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY, "&.Mui-disabled"],
        expectedStyle: { borderColor: "GrayText" },
    },
    {
        rule: "an Alert draws a CanvasText boundary",
        component: "MuiAlert",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY],
        expectedStyle: { border: "1px solid CanvasText" },
    },
    {
        rule: "a filled field draws a CanvasText boundary",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY],
        expectedStyle: { border: "1px solid CanvasText" },
    },
    {
        rule: "a Select menu draws a CanvasText boundary",
        component: "MuiMenu",
        slot: "paper",
        selectorPath: [FORCED_COLORS_QUERY],
        expectedStyle: { border: "1px solid CanvasText" },
    },
    {
        rule: "the selected menu option is the Highlight pair",
        component: "MuiMenuItem",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY, ENABLED_SELECTED_SELECTOR],
        expectedStyle: FORCED_SELECTED_OPTION_STYLE,
    },
    {
        rule: "the selected segment is the Highlight pair",
        component: "MuiToggleButton",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY, ENABLED_SELECTED_SELECTOR],
        expectedStyle: FORCED_SELECTED_OPTION_STYLE,
    },
    {
        rule: "the focus ring takes the Highlight system colour",
        component: "MuiButtonBase",
        slot: "root",
        selectorPath: [FOCUS_VISIBLE_SELECTOR, FORCED_COLORS_QUERY],
        expectedStyle: { outlineColor: "Highlight" },
    },
    {
        rule: "the selected menu option's ring is HighlightText 2 px inside its fill",
        component: "MuiMenuItem",
        slot: "root",
        selectorPath: [FORCED_COLORS_QUERY, "&.Mui-selected.Mui-focusVisible"],
        expectedStyle: { outlineColor: "HighlightText", outlineOffset: -5 },
    },
];

// The M3 titleSmall role on Roboto, which table header and total cells take.
const TITLE_SMALL_CELL_STYLE = {
    fontFamily: ROBOTO_FONT_STACK,
    fontSize: "14px",
    lineHeight: "20px",
    fontWeight: 500,
    letterSpacing: "0.1px",
};

// Component typography and spacing on the M3 type scale and the 4 dp grid.
const TYPE_AND_SPACING_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "helper text is bodySmall, 4 px below its field",
        component: "MuiFormHelperText",
        slot: "root",
        selectorPath: [],
        expectedStyle: {
            fontSize: "12px",
            lineHeight: "16px",
            letterSpacing: "0.4px",
            marginTop: "4px",
        },
    },
    {
        rule: "an Alert is padded 4 px and 16 px on an 8 px corner",
        component: "MuiAlert",
        slot: "root",
        selectorPath: [],
        expectedStyle: { padding: "4px 16px", borderRadius: 8 },
    },
    {
        rule: "an Alert icon fills a 20 px line box 8 px from the edge",
        component: "MuiAlert",
        slot: "icon",
        selectorPath: [],
        expectedStyle: { padding: "8px 0px", fontSize: "20px" },
    },
    {
        rule: "a filled field input is a 24 px line box with 24 px above and 8 px below",
        component: "MuiFilledInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR],
        expectedStyle: {
            height: "24px",
            lineHeight: "24px",
            paddingTop: "24px",
            paddingBottom: "8px",
        },
    },
    {
        rule: "a menu item pads its label 8 px above and below",
        component: "MuiMenuItem",
        slot: "root",
        selectorPath: [],
        expectedStyle: { paddingTop: "8px", paddingBottom: "8px" },
    },
    {
        rule: "a medium segment is at least 40 px tall with 8 px and 12 px padding, as a button",
        component: "MuiToggleButton",
        slot: "sizeMedium",
        selectorPath: [],
        expectedStyle: { minHeight: 40, padding: "8px 12px" },
    },
    {
        rule: "a table header cell is Roboto titleSmall",
        component: "MuiTableCell",
        slot: "head",
        selectorPath: [],
        expectedStyle: TITLE_SMALL_CELL_STYLE,
    },
    {
        rule: "a table total cell is Roboto titleSmall in on-surface",
        component: "MuiTableCell",
        slot: "footer",
        selectorPath: [],
        expectedStyle: { ...TITLE_SMALL_CELL_STYLE, color: "#1D1B20" },
    },
];

// The M3 text field's 16 dp inset, shared by the input text, the floating label and the helper.
const TEXT_FIELD_INSET = "16px";
const TEXT_FIELD_INSET_STYLE = { paddingLeft: TEXT_FIELD_INSET, paddingRight: TEXT_FIELD_INSET };
const ADORNED_START_INPUT_SELECTOR = ".MuiInputBase-adornedStart > &";
const ADORNED_END_INPUT_SELECTOR = ".MuiInputBase-adornedEnd > &";
// The notch edge sits 12 px in from the field's edge; the fieldset pads that less its outline,
// which is 1 px wide at rest and 2 px while the field has focus.
const RESTING_NOTCH_PADDING = "calc(12px - 1px)";
const FOCUSED_NOTCH_PADDING = "calc(12px - 2px)";
const NOTCH_LABEL_SELECTOR = "& > legend > span:not(.notranslate)";
const FOCUSED_NOTCHED_OUTLINE_SELECTOR = "&.Mui-focused .MuiOutlinedInput-notchedOutline";
// A label spans the field between the two insets; shrunk, its unscaled width is that over 0.75.
const RESTING_LABEL_MAX_WIDTH = "calc((100% - 2 * 16px) / 1)";
const SHRUNK_LABEL_MAX_WIDTH = "calc((100% - 2 * 16px) / 0.75)";
// A multiline field pads its textarea from its root: the 24 px line box the textarea inherits,
// and the 16 px inset on each side that holds no adornment, so an adorned side keeps MUI's spacing.
const MULTILINE_FIELD_STYLE = {
    lineHeight: "24px",
    "&:not(.MuiInputBase-adornedStart)": { paddingLeft: TEXT_FIELD_INSET },
    "&:not(.MuiInputBase-adornedEnd)": { paddingRight: TEXT_FIELD_INSET },
};

// Text-field horizontal geometry on the 4 dp grid, in place of MUI's 14 px, 12 px and 5 px values.
const TEXT_FIELD_INSET_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "an outlined field's input text sits 16 px in from either side",
        component: "MuiOutlinedInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR],
        expectedStyle: TEXT_FIELD_INSET_STYLE,
    },
    {
        rule: "a filled field's input text sits 16 px in from either side",
        component: "MuiFilledInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR],
        expectedStyle: TEXT_FIELD_INSET_STYLE,
    },
    {
        rule: "an outlined field's input keeps no padding beside a start adornment",
        component: "MuiOutlinedInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR, ADORNED_START_INPUT_SELECTOR],
        expectedStyle: { paddingLeft: 0 },
    },
    {
        rule: "an outlined field's input keeps no padding beside an end adornment",
        component: "MuiOutlinedInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR, ADORNED_END_INPUT_SELECTOR],
        expectedStyle: { paddingRight: 0 },
    },
    {
        rule: "a filled field's input keeps no padding beside a start adornment",
        component: "MuiFilledInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR, ADORNED_START_INPUT_SELECTOR],
        expectedStyle: { paddingLeft: 0 },
    },
    {
        rule: "a filled field's input keeps no padding beside an end adornment",
        component: "MuiFilledInput",
        slot: "input",
        selectorPath: [SINGLE_LINE_INPUT_SELECTOR, ADORNED_END_INPUT_SELECTOR],
        expectedStyle: { paddingRight: 0 },
    },
    {
        rule: "an outlined field's resting label sits on the 16 px inset",
        component: "MuiInputLabel",
        slot: "outlined",
        selectorPath: [],
        expectedStyle: {
            transform: "translate(16px, 16px) scale(1)",
            maxWidth: RESTING_LABEL_MAX_WIDTH,
        },
    },
    {
        rule: "a filled field's resting label sits on the 16 px inset",
        component: "MuiInputLabel",
        slot: "filled",
        selectorPath: [],
        expectedStyle: {
            transform: "translate(16px, 16px) scale(1)",
            maxWidth: RESTING_LABEL_MAX_WIDTH,
        },
    },
    {
        rule: "a resting outline puts its notch edge 12 px in from the field's edge",
        component: "MuiOutlinedInput",
        slot: "notchedOutline",
        selectorPath: [],
        expectedStyle: { paddingLeft: RESTING_NOTCH_PADDING, paddingRight: RESTING_NOTCH_PADDING },
    },
    {
        rule: "a focused outline keeps its notch edge 12 px in from the field's edge",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FOCUSED_NOTCHED_OUTLINE_SELECTOR],
        expectedStyle: { paddingLeft: FOCUSED_NOTCH_PADDING, paddingRight: FOCUSED_NOTCH_PADDING },
    },
    {
        rule: "the notch opens 4 px before a field's label and closes 4 px after it",
        component: "MuiOutlinedInput",
        slot: "notchedOutline",
        selectorPath: [NOTCH_LABEL_SELECTOR],
        expectedStyle: {
            paddingLeft: "4px",
            paddingRight: "4px",
            letterSpacing: "calc(0.5px * 0.75)",
        },
    },
    {
        rule: "an outlined or filled field's helper text sits 16 px in from either side",
        component: "MuiFormHelperText",
        slot: "contained",
        selectorPath: [],
        expectedStyle: { marginLeft: TEXT_FIELD_INSET, marginRight: TEXT_FIELD_INSET },
    },
];

// MUI 9.2.0's InputBase autofill detection, written out here rather than read from app/theme.ts:
// the animations its input runs and the keyframes they name, which MUI compares by these names.
const MUI_AUTO_FILL = "mui-auto-fill";
const MUI_AUTO_FILL_CANCEL = "mui-auto-fill-cancel";
const MUI_AUTOFILL_KEYFRAMES = {
    [`@keyframes ${MUI_AUTO_FILL}`]: { from: { animationName: MUI_AUTO_FILL } },
    [`@keyframes ${MUI_AUTO_FILL_CANCEL}`]: { from: { animationName: MUI_AUTO_FILL_CANCEL } },
};

// The InputBase input animations the theme restates once it stops InputBase injecting keyframes.
const AUTOFILL_DETECTION_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "a resting input starts MUI's 10 ms autofill cancel animation",
        component: "MuiInputBase",
        slot: "input",
        selectorPath: [],
        expectedStyle: { animationName: MUI_AUTO_FILL_CANCEL, animationDuration: "10ms" },
    },
    {
        rule: "an autofilled input starts MUI's 5000 s autofill animation",
        component: "MuiInputBase",
        slot: "input",
        selectorPath: ["&:-webkit-autofill"],
        expectedStyle: { animationName: MUI_AUTO_FILL, animationDuration: "5000s" },
    },
];

// The M3 text field's trailing error icon, written out here rather than read from app/theme.ts:
// MUI's ErrorOutline glyph, which the error Alert shows, as a 24 px icon 12 px in from the field's
// end. The field's text, and a label on the input's row, end 12 + 24 + 16 = 52 px in from it.
const ERROR_OUTLINE_PATH =
    "M11 15h2v2h-2zm0-8h2v6h-2zm.99-5C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 " +
    "12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8z";
const MASK_DATA_URI_PREFIX = 'url("data:image/svg+xml,';
const MASK_DATA_URI_SUFFIX = '")';
const FIELD_ERROR_SELECTOR = "&.Mui-error";
const ERROR_ROOM_INPUT_SELECTOR =
    "&:not(.MuiInputBase-adornedEnd) > .MuiInputBase-input:not(textarea):not(.MuiSelect-select)";
const ERROR_ROOM_ROOT_SELECTOR = "&.MuiInputBase-multiline, &.MuiInputBase-adornedEnd";
const ERROR_ICON_ROOM = { paddingRight: "52px" };
const ERROR_ICON_BOX = {
    content: '""',
    position: "absolute",
    width: 24,
    height: 24,
    right: "12px",
    pointerEvents: "none",
    maskRepeat: "no-repeat",
    maskPosition: "center",
    maskSize: "100% 100%",
    backgroundColor: "#B3261E",
};
// Disabled content is on-surface at 38 %. Forced colours keep the icon in a system colour.
const DISABLED_ERROR_ICON_COLOUR = "rgba(29, 27, 32, 0.38)";
const FORCED_ERROR_ICON_FILL = { forcedColorAdjust: "none", backgroundColor: "CanvasText" };
const FORCED_DISABLED_ERROR_ICON_FILL = { forcedColorAdjust: "none", backgroundColor: "GrayText" };
const RESTING_ERROR_LABEL_MAX_WIDTH = "calc((100% - 16px - 52px) / 1)";
const SHRUNK_ERROR_LABEL_MAX_WIDTH = "calc((100% - 16px - 52px) / 0.75)";
// The TextField state of a filled, non-Select field in error, whose root holds the icon.
const FILLED_ERROR_ICON_PROPS = { variant: "filled", error: true, select: false };

// A field in error shows a non-colour cue: the trailing error icon (QAVIS2-F09, QAUX1-F14).
const FIELD_ERROR_ICON_RULES: readonly ThemeRuleExpectation[] = [
    {
        rule: "an outlined field in error shows the error icon centred 12 px in from its end",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, "&::after"],
        expectedStyle: { ...ERROR_ICON_BOX, top: "50%", transform: "translateY(-50%)" },
    },
    {
        rule: "an outlined field's error icon is CanvasText under forced colours",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, "&::after", FORCED_COLORS_QUERY],
        expectedStyle: FORCED_ERROR_ICON_FILL,
    },
    {
        rule: "a disabled outlined field's error icon is disabled content",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, "&.Mui-disabled::after"],
        expectedStyle: { backgroundColor: DISABLED_ERROR_ICON_COLOUR },
    },
    {
        rule: "a disabled outlined field's error icon is GrayText under forced colours",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, "&.Mui-disabled::after", FORCED_COLORS_QUERY],
        expectedStyle: FORCED_DISABLED_ERROR_ICON_FILL,
    },
    {
        rule: "an outlined field's input in error ends its text 52 px in, clear of the icon",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, ERROR_ROOM_INPUT_SELECTOR],
        expectedStyle: ERROR_ICON_ROOM,
    },
    {
        rule: "a multiline or end-adorned outlined field in error keeps the icon's room itself",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, ERROR_ROOM_ROOT_SELECTOR],
        expectedStyle: ERROR_ICON_ROOM,
    },
    {
        rule: "a Select in error shows the icon in its arrow's place, 8 px in from its end",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, "&.MuiSelect-root::after"],
        expectedStyle: { right: "8px" },
    },
    {
        rule: "a Select in error hides its arrow under the icon",
        component: "MuiOutlinedInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, "&.MuiSelect-root .MuiSelect-icon"],
        expectedStyle: { display: "none" },
    },
    {
        rule: "a filled field's input in error ends its text 52 px in, clear of the icon",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, ERROR_ROOM_INPUT_SELECTOR],
        expectedStyle: ERROR_ICON_ROOM,
    },
    {
        rule: "a multiline or end-adorned filled field in error keeps the icon's room itself",
        component: "MuiFilledInput",
        slot: "root",
        selectorPath: [FIELD_ERROR_SELECTOR, ERROR_ROOM_ROOT_SELECTOR],
        expectedStyle: ERROR_ICON_ROOM,
    },
    {
        rule: "an outlined field's resting label in error ends clear of the icon",
        component: "MuiInputLabel",
        slot: "outlined",
        selectorPath: [FIELD_ERROR_SELECTOR],
        expectedStyle: { maxWidth: RESTING_ERROR_LABEL_MAX_WIDTH },
    },
    {
        rule: "a filled field's resting label in error ends clear of the icon",
        component: "MuiInputLabel",
        slot: "filled",
        selectorPath: [FIELD_ERROR_SELECTOR],
        expectedStyle: { maxWidth: RESTING_ERROR_LABEL_MAX_WIDTH },
    },
];

// The filled field's icon on its TextField root at each field height, and each label placement's
// width in error.
const FIELD_ERROR_ICON_VARIANTS: readonly SlotVariantExpectation[] = [
    {
        rule: "a filled field in error shows the error icon 16 px from its top, 12 px from its end",
        component: "MuiTextField",
        slot: "root",
        variantProps: FILLED_ERROR_ICON_PROPS,
        expectedStyle: {
            "&::after": {
                ...ERROR_ICON_BOX,
                top: "16px",
                [FORCED_COLORS_QUERY]: FORCED_ERROR_ICON_FILL,
            },
        },
    },
    {
        rule: "a small filled field's error icon is centred 12 px from its top",
        component: "MuiTextField",
        slot: "root",
        variantProps: { variant: "filled", error: true, size: "small" },
        expectedStyle: { "&::after": { top: "12px" } },
    },
    {
        rule: "a filled field with a hidden label centres its error icon 16 px from its top",
        component: "MuiTextField",
        slot: "root",
        variantProps: { variant: "filled", error: true, hiddenLabel: true },
        expectedStyle: { "&::after": { top: "16px" } },
    },
    {
        rule: "a small filled field with a hidden label centres its error icon 8 px from its top",
        component: "MuiTextField",
        slot: "root",
        variantProps: { variant: "filled", error: true, hiddenLabel: true, size: "small" },
        expectedStyle: { "&::after": { top: "8px" } },
    },
    {
        rule: "a disabled filled field's error icon is disabled content, or GrayText when forced",
        component: "MuiTextField",
        slot: "root",
        variantProps: { variant: "filled", error: true, disabled: true },
        expectedStyle: {
            "&::after": {
                backgroundColor: DISABLED_ERROR_ICON_COLOUR,
                [FORCED_COLORS_QUERY]: FORCED_DISABLED_ERROR_ICON_FILL,
            },
        },
    },
    {
        rule: "a small outlined field's resting label in error ends clear of the icon",
        component: "MuiInputLabel",
        slot: "outlined",
        variantProps: { size: "small" },
        expectedStyle: { [FIELD_ERROR_SELECTOR]: { maxWidth: RESTING_ERROR_LABEL_MAX_WIDTH } },
    },
    {
        rule: "an outlined label shrunk into the notch keeps its width in error, above the icon",
        component: "MuiInputLabel",
        slot: "outlined",
        variantProps: { shrink: true },
        expectedStyle: { [FIELD_ERROR_SELECTOR]: { maxWidth: SHRUNK_LABEL_MAX_WIDTH } },
    },
    {
        rule: "a small filled field's resting label in error ends clear of the icon",
        component: "MuiInputLabel",
        slot: "filled",
        variantProps: { size: "small" },
        expectedStyle: { [FIELD_ERROR_SELECTOR]: { maxWidth: RESTING_ERROR_LABEL_MAX_WIDTH } },
    },
    {
        rule: "a filled field's shrunk label in error ends clear of the icon",
        component: "MuiInputLabel",
        slot: "filled",
        variantProps: { shrink: true },
        expectedStyle: { [FIELD_ERROR_SELECTOR]: { maxWidth: SHRUNK_ERROR_LABEL_MAX_WIDTH } },
    },
    {
        rule: "a small filled field's shrunk label in error ends clear of the icon",
        component: "MuiInputLabel",
        slot: "filled",
        variantProps: { shrink: true, size: "small" },
        expectedStyle: { [FIELD_ERROR_SELECTOR]: { maxWidth: SHRUNK_ERROR_LABEL_MAX_WIDTH } },
    },
];

const THEME_RULE_GROUPS: readonly [string, readonly ThemeRuleExpectation[]][] = [
    ["M3 outlines, fills and dividers", OUTLINE_AND_FILL_RULES],
    ["M3 focus ring", FOCUS_RING_RULES],
    ["M3 touch targets", TOUCH_TARGET_RULES],
    ["Forced colours", FORCED_COLOUR_RULES],
    ["M3 component typography and spacing", TYPE_AND_SPACING_RULES],
    ["M3 text-field insets (FE2A-F12)", TEXT_FIELD_INSET_RULES],
    ["MUI autofill detection (QPERF2-F01)", AUTOFILL_DETECTION_RULES],
    ["M3 field error icon (QAVIS2-F09, QAUX1-F14)", FIELD_ERROR_ICON_RULES],
];

// Overrides MUI applies only to components in one state: a small or hidden-label field, a dense
// menu item, a normally padded small cell, a severity.
const SLOT_VARIANT_EXPECTATIONS: readonly SlotVariantExpectation[] = [
    {
        rule: "a small filled field's input sits 20 px below the top and 4 px above the bottom",
        component: "MuiFilledInput",
        slot: "input",
        variantProps: { size: "small" },
        expectedStyle: {
            [SINGLE_LINE_INPUT_SELECTOR]: { paddingTop: "20px", paddingBottom: "4px" },
        },
    },
    {
        rule: "a filled field with a hidden label centres its input 16 px from top and bottom",
        component: "MuiFilledInput",
        slot: "input",
        variantProps: { hiddenLabel: true },
        expectedStyle: {
            [SINGLE_LINE_INPUT_SELECTOR]: { paddingTop: "16px", paddingBottom: "16px" },
        },
    },
    {
        rule: "a small filled field with a hidden label centres its input 8 px from top and bottom",
        component: "MuiFilledInput",
        slot: "input",
        variantProps: { hiddenLabel: true, size: "small" },
        expectedStyle: {
            [SINGLE_LINE_INPUT_SELECTOR]: { paddingTop: "8px", paddingBottom: "8px" },
        },
    },
    {
        rule: "a multiline filled field pads a 24 px line box as a filled input, 16 px in",
        component: "MuiFilledInput",
        slot: "root",
        variantProps: { multiline: true },
        expectedStyle: { ...MULTILINE_FIELD_STYLE, paddingTop: "24px", paddingBottom: "8px" },
    },
    {
        rule: "a small multiline filled field sits 20 px below the top and 4 px above the bottom",
        component: "MuiFilledInput",
        slot: "root",
        variantProps: { multiline: true, size: "small" },
        expectedStyle: { paddingTop: "20px", paddingBottom: "4px" },
    },
    {
        rule: "a multiline filled field with a hidden label centres its text 16 px from each edge",
        component: "MuiFilledInput",
        slot: "root",
        variantProps: { multiline: true, hiddenLabel: true },
        expectedStyle: { paddingTop: "16px", paddingBottom: "16px" },
    },
    {
        rule: "a small multiline filled field with a hidden label centres its text 8 px in",
        component: "MuiFilledInput",
        slot: "root",
        variantProps: { multiline: true, hiddenLabel: true, size: "small" },
        expectedStyle: { paddingTop: "8px", paddingBottom: "8px" },
    },
    {
        rule: "a multiline outlined field pads a 24 px line box as an outlined input, 16 px in",
        component: "MuiOutlinedInput",
        slot: "root",
        variantProps: { multiline: true },
        expectedStyle: { ...MULTILINE_FIELD_STYLE, paddingTop: "16px", paddingBottom: "16px" },
    },
    {
        rule: "a small multiline outlined field pads its 24 px line box 8 px above and below",
        component: "MuiOutlinedInput",
        slot: "root",
        variantProps: { multiline: true, size: "small" },
        expectedStyle: { paddingTop: "8px", paddingBottom: "8px" },
    },
    {
        rule: "a native multiline textarea grows to its wrapped value",
        component: "MuiInputBase",
        slot: "input",
        variantProps: { multiline: true, inputComponent: "textarea" },
        expectedStyle: { fieldSizing: "content" },
    },
    {
        rule: "a dense menu item keeps MUI's 4 px above and below its label",
        component: "MuiMenuItem",
        slot: "root",
        variantProps: { dense: true },
        expectedStyle: { paddingTop: "4px", paddingBottom: "4px" },
    },
    {
        rule: "a normally padded small cell is padded 8 px and 16 px",
        component: "MuiTableCell",
        slot: "sizeSmall",
        variantProps: { padding: "normal" },
        expectedStyle: { padding: "8px 16px" },
    },
    {
        rule: "a small field's input spans the 48 px target and overhangs its 40 px outline",
        component: "MuiOutlinedInput",
        slot: "input",
        variantProps: { size: "small" },
        expectedStyle: {
            [SINGLE_LINE_INPUT_SELECTOR]: {
                paddingTop: "12px",
                paddingBottom: "12px",
                marginTop: -4,
                marginBottom: -4,
            },
        },
    },
    {
        rule: "an error Alert is red error text and icon on the error container",
        component: "MuiAlert",
        slot: "root",
        variantProps: { variant: "standard", colorSeverity: "error" },
        expectedStyle: {
            backgroundColor: "#F9DEDC",
            color: "#B3261E",
            "& .MuiAlert-icon": { color: "#B3261E" },
        },
    },
    {
        rule: "an info Alert is the primary-container pair with a primary icon",
        component: "MuiAlert",
        slot: "root",
        variantProps: { variant: "standard", colorSeverity: "info" },
        expectedStyle: {
            backgroundColor: PRIMARY_CONTAINER_HEX,
            color: ON_PRIMARY_CONTAINER_HEX,
            "& .MuiAlert-icon": { color: "#6750A4" },
        },
    },
    {
        rule: "a small outlined field's resting label sits on the 16 px inset at MUI's 9 px",
        component: "MuiInputLabel",
        slot: "outlined",
        variantProps: { size: "small" },
        expectedStyle: { transform: "translate(16px, 9px) scale(1)" },
    },
    {
        rule: "an outlined field's shrunk label sits on the 16 px inset at MUI's -9 px",
        component: "MuiInputLabel",
        slot: "outlined",
        variantProps: { shrink: true },
        expectedStyle: {
            transform: "translate(16px, -9px) scale(0.75)",
            maxWidth: SHRUNK_LABEL_MAX_WIDTH,
        },
    },
    {
        rule: "a small filled field's resting label sits on the 16 px inset at MUI's 13 px",
        component: "MuiInputLabel",
        slot: "filled",
        variantProps: { size: "small" },
        expectedStyle: { transform: "translate(16px, 13px) scale(1)" },
    },
    {
        rule: "a filled field's shrunk label sits on the 16 px inset at MUI's 7 px",
        component: "MuiInputLabel",
        slot: "filled",
        variantProps: { shrink: true },
        expectedStyle: {
            transform: "translate(16px, 7px) scale(0.75)",
            maxWidth: SHRUNK_LABEL_MAX_WIDTH,
        },
    },
    {
        rule: "a small filled field's shrunk label sits on the 16 px inset at MUI's 4 px",
        component: "MuiInputLabel",
        slot: "filled",
        variantProps: { shrink: true, size: "small" },
        expectedStyle: {
            transform: "translate(16px, 4px) scale(0.75)",
            maxWidth: SHRUNK_LABEL_MAX_WIDTH,
        },
    },
    {
        rule: "an outlined Select's arrow sits 8 px in from the field's edge",
        component: "MuiSelect",
        slot: "icon",
        variantProps: { variant: "outlined" },
        expectedStyle: { right: "8px" },
    },
    {
        rule: "a filled Select's arrow sits 8 px in from the field's edge",
        component: "MuiSelect",
        slot: "icon",
        variantProps: { variant: "filled" },
        expectedStyle: { right: "8px" },
    },
];

// The label states of each field variant in MUI's order, so a later, more specific state wins.
const LABEL_PLACEMENT_ORDERS: readonly [string, readonly StyleRecord[]][] = [
    ["outlined", [{ size: "small" }, { shrink: true }]],
    ["filled", [{ size: "small" }, { shrink: true }, { shrink: true, size: "small" }]],
];

const NO_FOCUS_RIPPLE_COMPONENTS = ["MuiButton", "MuiToggleButton", "MuiIconButton"] as const;

// The M3 elevation levels 1-5 and the MUI elevation indices that render each one.
const M3_LEVEL_INDEX_RANGES = [
    {
        shadow: "0px 1px 2px 0px rgba(0, 0, 0, 0.3), 0px 1px 3px 1px rgba(0, 0, 0, 0.15)",
        first: 1,
        last: 2,
    },
    {
        shadow: "0px 1px 2px 0px rgba(0, 0, 0, 0.3), 0px 2px 6px 2px rgba(0, 0, 0, 0.15)",
        first: 3,
        last: 4,
    },
    {
        shadow: "0px 1px 3px 0px rgba(0, 0, 0, 0.3), 0px 4px 8px 3px rgba(0, 0, 0, 0.15)",
        first: 5,
        last: 8,
    },
    {
        shadow: "0px 2px 3px 0px rgba(0, 0, 0, 0.3), 0px 6px 10px 4px rgba(0, 0, 0, 0.15)",
        first: 9,
        last: 16,
    },
    {
        shadow: "0px 4px 4px 0px rgba(0, 0, 0, 0.3), 0px 8px 12px 6px rgba(0, 0, 0, 0.15)",
        first: 17,
        last: 24,
    },
];
const MUI_SHADOW_COUNT = 25;

// MUI's documented visuallyHidden helper (@mui/utils/visuallyHidden). The 1px and -1px values are
// strings: in sx a bare 1 would be 100% and a bare margin would be theme spacing.
const MUI_VISUALLY_HIDDEN_VALUES = {
    border: 0,
    clipPath: "inset(50%)",
    height: "1px",
    margin: "-1px",
    overflow: "hidden",
    padding: 0,
    position: "absolute",
    whiteSpace: "nowrap",
    width: "1px",
};

// Each MUI breakpoint the theme keeps: a row aligns by its bottom edge from that breakpoint up and
// keeps its default alignment below it, so the style holds that one breakpoint key.
const ROW_END_ALIGNMENT_CASES: readonly (readonly [Breakpoint, StyleRecord])[] = [
    ["xs", { alignItems: { xs: "flex-end" } }],
    ["sm", { alignItems: { sm: "flex-end" } }],
    ["md", { alignItems: { md: "flex-end" } }],
    ["lg", { alignItems: { lg: "flex-end" } }],
    ["xl", { alignItems: { xl: "flex-end" } }],
];

// The list tables' scrolling region: edge shadows in the shadow role under 40 px paper-coloured
// covers that scroll with the content, so only an edge with hidden content shows its 12 px shadow;
// a thin outline-role scrollbar; and the M3 focus ring, 3 px secondary 2 px outside the region.
const SCROLL_REGION_VALUES = {
    backgroundImage: [
        "linear-gradient(to right, #F3EDF7 30%, transparent)",
        "linear-gradient(to left, #F3EDF7 30%, transparent)",
        "linear-gradient(to right, rgba(0, 0, 0, 0.3), transparent)",
        "linear-gradient(to left, rgba(0, 0, 0, 0.3), transparent)",
    ].join(", "),
    backgroundPosition: "left center, right center, left center, right center",
    backgroundSize: "40px 100%, 40px 100%, 12px 100%, 12px 100%",
    backgroundRepeat: "no-repeat",
    backgroundAttachment: "local, local, scroll, scroll",
    scrollbarWidth: "thin",
    scrollbarColor: "#79747E transparent",
    "&:focus-visible": {
        outline: "3px solid #625B71",
        outlineOffset: 2,
        "@media (forced-colors: active)": { outlineColor: "Highlight" },
    },
};

// The list tables' fixed layout: every column keeps its declared width and a wider text breaks
// inside its column. The minimum width is the columns' total in 4 dp units: the CCLI, CU00 and
// CT00 totals.
const FIXED_TABLE_VALUES = {
    tableLayout: "fixed",
    "& .MuiTableCell-root": { overflowWrap: "anywhere" },
};
const FIXED_TABLE_MIN_WIDTHS: readonly [number, string][] = [
    [121, "484px"],
    [172, "688px"],
    [173, "692px"],
];

// An sx style written as a theme callback resolves against the CardDemo theme.
function ResolveSx(style: SxProps<Theme>): unknown {
    return typeof style === "function" ? style(CARD_DEMO_THEME) : style;
}

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

function ReadComponentEntry(component: string): ComponentThemeEntry | undefined {
    const components = CARD_DEMO_THEME.components as
        Record<string, ComponentThemeEntry> | undefined;
    return components?.[component];
}

// Resolves one style-override slot of a component and walks it down a chain of nested selectors.
function ReadThemeRule(
    component: string,
    slot: string,
    selectorPath: readonly string[],
): StyleRecord | undefined {
    const slotStyle = ReadComponentEntry(component)?.styleOverrides?.[slot];
    const styleProps = { theme: CARD_DEMO_THEME, ownerState: {} };
    return selectorPath.reduce<StyleRecord | undefined>(
        (rule, selector) => AsStyleRecord(rule?.[selector]),
        AsStyleRecord(ResolveStyle(slotStyle, styleProps)),
    );
}

function ReadSlotVariants(component: string, slot: string): readonly StyleVariantEntry[] {
    const variants = ReadThemeRule(component, slot, [])?.variants;
    return Array.isArray(variants) ? (variants as StyleVariantEntry[]) : [];
}

function MatchesVariantProps(entry: StyleVariantEntry, variantProps: StyleRecord): boolean {
    return Object.entries(variantProps).every(([key, value]) => entry.props[key] === value);
}

function FindSlotVariantStyle(
    component: string,
    slot: string,
    variantProps: StyleRecord,
): StyleRecord | undefined {
    const variants = ReadSlotVariants(component, slot);
    return variants.find((entry) => MatchesVariantProps(entry, variantProps))?.style;
}

// The SVG a CSS url() holding an SVG data URI carries, or "" for any other value.
function DecodeMaskSvg(maskImage: unknown): string {
    const isSvgDataUri =
        typeof maskImage === "string" &&
        maskImage.startsWith(MASK_DATA_URI_PREFIX) &&
        maskImage.endsWith(MASK_DATA_URI_SUFFIX);
    if (!isSvgDataUri) {
        return "";
    }
    return decodeURIComponent(
        maskImage.slice(MASK_DATA_URI_PREFIX.length, -MASK_DATA_URI_SUFFIX.length),
    );
}

// The error icon of each field variant: on an outlined field's root, on a filled field's TextField.
const ERROR_ICON_HOSTS: readonly [string, () => StyleRecord | undefined][] = [
    [
        "outlined",
        () => ReadThemeRule("MuiOutlinedInput", "root", [FIELD_ERROR_SELECTOR, "&::after"]),
    ],
    [
        "filled",
        () =>
            AsStyleRecord(
                FindSlotVariantStyle("MuiTextField", "root", FILLED_ERROR_ICON_PROPS)?.["&::after"],
            ),
    ],
];

function ExpectedShadowAt(muiIndex: number): string {
    const levelRange = M3_LEVEL_INDEX_RANGES.find(
        ({ first, last }) => muiIndex >= first && muiIndex <= last,
    );
    return levelRange?.shadow ?? "none";
}

const SHADOW_EXPECTATIONS: readonly ShadowExpectation[] = Array.from(
    { length: MUI_SHADOW_COUNT },
    (_unused, muiIndex) => ({ muiIndex, expectedShadow: ExpectedShadowAt(muiIndex) }),
);

describe("M3 colour roles (AAP 0.5.3)", () => {
    it.each(M3_COLOR_EXPECTATIONS)("$token is $expected", ({ read, expected }) => {
        expect(NormaliseColour(read(CARD_DEMO_THEME))).toBe(expected.toUpperCase());
    });

    it("palette.m3 holds only the augmented token-table roles", () => {
        const augmentedRoles = Object.keys(CARD_DEMO_THEME.palette.m3).sort();
        expect(augmentedRoles).toEqual(M3_AUGMENTED_COLOUR_ROLES);
    });

    it(`text.disabled is the ${ON_SURFACE_DISABLED_CONTENT} on-surface disabled content`, () => {
        expect(CARD_DEMO_THEME.palette.text.disabled).toBe(ON_SURFACE_DISABLED_CONTENT);
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
        "$variant is Roboto $fontSize px on a $lineHeight px line " +
            "at weight $fontWeight, tracking $letterSpacing",
        ({ variant, fontSize, lineHeight, fontWeight, letterSpacing }) => {
            const roleStyle = CARD_DEMO_THEME.typography[variant];
            expect(roleStyle).toBeDefined();
            const measuredFontSize = FontSizeToPixels(roleStyle.fontSize);
            const measuredLineHeight = LineHeightToPixels(roleStyle.lineHeight, measuredFontSize);
            expect(measuredFontSize).toBeCloseTo(fontSize, PIXEL_PRECISION_DIGITS);
            expect(measuredLineHeight).toBeCloseTo(lineHeight, PIXEL_PRECISION_DIGITS);
            expect(roleStyle.fontWeight).toBe(fontWeight);
            expect(roleStyle.letterSpacing).toBe(letterSpacing);
            expect(roleStyle.fontFamily).toBe(ROBOTO_FONT_STACK);
        },
    );
});

describe("MUI built-in variants on the M3 type scale (FE2A-F12)", () => {
    it.each(BUILT_IN_VARIANT_EXPECTATIONS)(
        "$variant renders the $role role",
        ({ variant, role }) => {
            expect(CARD_DEMO_THEME.typography[variant]).toMatchObject(
                CARD_DEMO_THEME.typography[role],
            );
        },
    );

    it("the button variant keeps legacy labels in their own case", () => {
        expect(CARD_DEMO_THEME.typography.button.textTransform).toBe("none");
    });
});

describe("M3 hover state layer (FE2C-F01)", () => {
    it(`action.hover is on-surface at ${M3_HOVER_OPACITY} opacity`, () => {
        expect(CARD_DEMO_THEME.palette.action.hoverOpacity).toBe(M3_HOVER_OPACITY);
        expect(CARD_DEMO_THEME.palette.action.hover).toBe(ON_SURFACE_HOVER_LAYER);
    });
});

describe("M3 action roles (FE2C-F01)", () => {
    it.each(ACTION_ROLE_EXPECTATIONS)("action.$key is $expected", ({ key, expected }) => {
        expect(CARD_DEMO_THEME.palette.action[key]).toBe(expected);
    });
});

describe.each(THEME_RULE_GROUPS)("%s", (_groupName, rules) => {
    it.each(rules)("$rule", ({ component, slot, selectorPath, expectedStyle }) => {
        expect(ReadThemeRule(component, slot, selectorPath)).toMatchObject(expectedStyle);
    });
});

describe("State-scoped overrides (FE2A-F10, FE2A-F12)", () => {
    it.each(SLOT_VARIANT_EXPECTATIONS)(
        "$rule",
        ({ component, slot, variantProps, expectedStyle }) => {
            expect(FindSlotVariantStyle(component, slot, variantProps)).toMatchObject(
                expectedStyle,
            );
        },
    );

    it("only the error and info Alerts change colour, so success keeps MUI's green", () => {
        const severities = ReadSlotVariants("MuiAlert", "root").map(
            (entry) => entry.props.colorSeverity,
        );
        expect(severities).toEqual(["error", "info"]);
    });
});

describe("Floating-label placements (FE2A-F12)", () => {
    it.each(LABEL_PLACEMENT_ORDERS)(
        "a shrunk %s label outranks its small resting placement",
        (slot, placementProps) => {
            const placements = ReadSlotVariants("MuiInputLabel", slot).map((entry) => entry.props);
            expect(placements).toEqual(placementProps);
        },
    );
});

describe("Field error icon placements (QAVIS2-F09, QAUX1-F14)", () => {
    it.each(FIELD_ERROR_ICON_VARIANTS)(
        "$rule",
        ({ component, slot, variantProps, expectedStyle }) => {
            expect(FindSlotVariantStyle(component, slot, variantProps)).toMatchObject(
                expectedStyle,
            );
        },
    );
});

describe("Field error icon glyph and hosts (QAVIS2-F09, QAUX1-F14)", () => {
    it.each(ERROR_ICON_HOSTS)(
        "the %s field's error icon is MUI's ErrorOutline glyph",
        (_hostName, read) => {
            const iconStyle = read();
            const glyph = DecodeMaskSvg(iconStyle?.maskImage);
            expect(glyph).toContain('viewBox="0 0 24 24"');
            expect(glyph).toContain(`<path d="${ERROR_OUTLINE_PATH}"/>`);
            expect(iconStyle?.WebkitMaskImage).toBe(iconStyle?.maskImage);
        },
    );

    it("only a filled, non-Select TextField in error draws the icon on its root", () => {
        const variants = ReadSlotVariants("MuiTextField", "root");
        const iconRows = variants.filter(
            (entry) => AsStyleRecord(entry.style["&::after"])?.content !== undefined,
        );
        expect(variants.every(({ props }) => props.variant === "filled" && props.error)).toBe(true);
        expect(iconRows.map((entry) => entry.props)).toEqual([FILLED_ERROR_ICON_PROPS]);
    });
});

describe("Small table cells on the 4 dp grid (FE2A-F12)", () => {
    it("small checkbox and unpadded cells keep MUI's padding", () => {
        const paddedStates = ReadSlotVariants("MuiTableCell", "sizeSmall").map(
            (entry) => entry.props.padding,
        );
        expect(ReadThemeRule("MuiTableCell", "sizeSmall", [])).not.toHaveProperty("padding");
        expect(paddedStates).toEqual(["normal"]);
    });
});

describe("Focus ripple (FE1A-F02, FE2A-F05, FE2C-F05)", () => {
    it.each(NO_FOCUS_RIPPLE_COMPONENTS)("%s turns the pulsing focus ripple off", (component) => {
        expect(ReadComponentEntry(component)?.defaultProps?.disableFocusRipple).toBe(true);
    });
});

describe("Autofill keyframes inserted once per app (QPERF2-F01)", () => {
    it("InputBase stops injecting its own autofill keyframes on every mount", () => {
        const inputBaseDefaults = ReadComponentEntry("MuiInputBase")?.defaultProps;
        expect(inputBaseDefaults?.disableInjectingGlobalStyles).toBe(true);
    });

    it("AUTOFILL_KEYFRAMES_STYLE holds MUI's two autofill keyframes", () => {
        expect(AUTOFILL_KEYFRAMES_STYLE).toEqual(MUI_AUTOFILL_KEYFRAMES);
    });
});

describe("M3 elevation (FE2A-F12)", () => {
    it(`shadows holds MUI's ${MUI_SHADOW_COUNT} elevation indices`, () => {
        expect(CARD_DEMO_THEME.shadows).toHaveLength(MUI_SHADOW_COUNT);
    });

    it.each(SHADOW_EXPECTATIONS)(
        "shadows[$muiIndex] is the M3 level the index maps to",
        ({ muiIndex, expectedShadow }) => {
            expect(CARD_DEMO_THEME.shadows[muiIndex]).toBe(expectedShadow);
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

describe("Visually hidden text", () => {
    it("VISUALLY_HIDDEN_STYLE carries MUI's visuallyHidden values", () => {
        expect(VISUALLY_HIDDEN_STYLE).toEqual(MUI_VISUALLY_HIDDEN_VALUES);
    });
});

describe("Row shares", () => {
    it("RowShareStyle grows an item from no width to its shares of the row", () => {
        expect(RowShareStyle(4)).toEqual({ flex: 4 });
    });
});

describe("Row end alignment", () => {
    it.each(ROW_END_ALIGNMENT_CASES)(
        "RowEndAlignmentFrom(%s) aligns a row by its bottom edge from that breakpoint up",
        (breakpoint, expectedStyle) => {
            expect(RowEndAlignmentFrom(breakpoint)).toEqual(expectedStyle);
        },
    );
});

describe("List table scrolling regions", () => {
    it("SCROLL_REGION_STYLE draws edge cues, an outline scrollbar and the M3 focus ring", () => {
        expect(ResolveSx(SCROLL_REGION_STYLE)).toEqual(SCROLL_REGION_VALUES);
    });
});

// A fixed layout at least as wide as its columns, whose texts break inside their column.
describe("Fixed-layout list tables", () => {
    it.each(FIXED_TABLE_MIN_WIDTHS)(
        "FixedLayoutTableStyle(%i) is a fixed layout at least %s wide",
        (minWidthUnits, minWidth) => {
            const tableStyle = FixedLayoutTableStyle(minWidthUnits) as Record<string, unknown>;
            const { minWidth: minWidthOf, ...fixedValues } = tableStyle;
            expect(fixedValues).toEqual(FIXED_TABLE_VALUES);
            expect(typeof minWidthOf === "function" && minWidthOf(CARD_DEMO_THEME)).toBe(minWidth);
        },
    );
});
