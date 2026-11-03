// Material Design 3 token theme for CardDemo (AAP 0.5); titles per COTTL01Y.
import type { CSSProperties } from "react";
import {
    alpha,
    createTheme,
    type CSSObject,
    type Shadows,
    type SxProps,
    type Theme,
    type TypographyStyle,
    type TypographyVariant,
} from "@mui/material/styles";
import type { M3ShapeScale } from "@/theme/m3";

/**
 * Material Design 3 baseline colour roles. They replace the 3270 COLOR attributes of the BMS
 * maps and are the only colour values in the frontend.
 */
export const M3_COLOR_TOKENS = {
    primary: "#6750A4",
    onPrimary: "#FFFFFF",
    primaryContainer: "#EADDFF",
    onPrimaryContainer: "#4F378B",
    secondary: "#625B71",
    tertiary: "#7D5260",
    error: "#B3261E",
    errorContainer: "#F9DEDC",
    onErrorContainer: "#8C1D18",
    surface: "#FEF7FF",
    surfaceContainer: "#F3EDF7",
    surfaceContainerHighest: "#E6E0E9",
    onSurface: "#1D1B20",
    onSurfaceVariant: "#49454F",
    outline: "#79747E",
    outlineVariant: "#CAC4D0",
    shadow: "#000000",
} as const;

/** Material Design 3 corner-radius scale, in px. */
export const M3_SHAPE_SCALE: M3ShapeScale = {
    extraSmall: 4,
    small: 8,
    medium: 12,
    large: 16,
    extraLarge: 28,
};

/** The size, line height, weight and tracking of one Material Design 3 type role. */
interface M3TypeRoleStyle {
    fontSize: string;
    lineHeight: string;
    fontWeight: number;
    letterSpacing: string;
}

/** Material Design 3 type scale: the 15 display, headline, title, body and label roles. */
export const M3_TYPE_SCALE = {
    displayLarge: {
        fontSize: "57px",
        lineHeight: "64px",
        fontWeight: 400,
        letterSpacing: "-0.25px",
    },
    displayMedium: { fontSize: "45px", lineHeight: "52px", fontWeight: 400, letterSpacing: "0px" },
    displaySmall: { fontSize: "36px", lineHeight: "44px", fontWeight: 400, letterSpacing: "0px" },
    headlineLarge: { fontSize: "32px", lineHeight: "40px", fontWeight: 400, letterSpacing: "0px" },
    headlineMedium: { fontSize: "28px", lineHeight: "36px", fontWeight: 400, letterSpacing: "0px" },
    headlineSmall: { fontSize: "24px", lineHeight: "32px", fontWeight: 400, letterSpacing: "0px" },
    titleLarge: { fontSize: "22px", lineHeight: "28px", fontWeight: 400, letterSpacing: "0px" },
    titleMedium: { fontSize: "16px", lineHeight: "24px", fontWeight: 500, letterSpacing: "0.15px" },
    titleSmall: { fontSize: "14px", lineHeight: "20px", fontWeight: 500, letterSpacing: "0.1px" },
    bodyLarge: { fontSize: "16px", lineHeight: "24px", fontWeight: 400, letterSpacing: "0.5px" },
    bodyMedium: { fontSize: "14px", lineHeight: "20px", fontWeight: 400, letterSpacing: "0.25px" },
    bodySmall: { fontSize: "12px", lineHeight: "16px", fontWeight: 400, letterSpacing: "0.4px" },
    labelLarge: { fontSize: "14px", lineHeight: "20px", fontWeight: 500, letterSpacing: "0.1px" },
    labelMedium: { fontSize: "12px", lineHeight: "16px", fontWeight: 500, letterSpacing: "0.5px" },
    labelSmall: { fontSize: "11px", lineHeight: "16px", fontWeight: 500, letterSpacing: "0.5px" },
} as const satisfies Record<string, M3TypeRoleStyle>;

type M3TypeRole = keyof typeof M3_TYPE_SCALE;

// The Roboto face that layout.tsx loads through next/font and exposes as --font-roboto. The
// var() fallback keeps the declaration valid where the variable is undefined: a bare
// var(--font-roboto) would void the whole stack and drop text to the browser's serif default.
const FONT_FAMILY = "var(--font-roboto, Roboto), Roboto, Helvetica, Arial, sans-serif";

// The 4 dp grid: theme.spacing(4) is 16px.
const SPACING_UNIT_PX = 4;

// The M3 state-layer opacities: hover 8 %, and the 4 % container of a disabled filled field.
const M3_HOVER_STATE_LAYER_OPACITY = 0.08;
const M3_DISABLED_CONTAINER_OPACITY = 0.04;
// The M3 focus state layer, and the on-surface content (38 %) and the container or outline (12 %)
// of a disabled control.
const M3_FOCUS_STATE_LAYER_OPACITY = 0.1;
const M3_DISABLED_CONTENT_OPACITY = 0.38;
const M3_DISABLED_CONTROL_CONTAINER_OPACITY = 0.12;

// MUI paints the resting outline of an outlined field grey again while a touch device keeps a
// hover after a tap. This selector re-applies the outline role there, and only on a field no
// focused, error or disabled rule colours, so those states keep their own outline.
const TOUCH_HOVER_RESTING_OUTLINE_SELECTOR =
    "&:hover:not(.Mui-focused):not(.Mui-error):not(.Mui-disabled) .MuiOutlinedInput-notchedOutline";

// The M3 focus indicator: a 3 dp ring in the secondary role, 2 dp outside the component.
const M3_FOCUS_RING_WIDTH_PX = 3;
const M3_FOCUS_RING_OFFSET_PX = 2;
const FOCUS_VISIBLE_SELECTOR = "&.Mui-focusVisible";

/**
 * The ring drawn inside the component instead: list and menu items fill their container edge to
 * edge, and the pagination arrows sit in a scrolling toolbar, so a ring outside them is clipped.
 */
const INSET_FOCUS_RING_STYLE: CSSObject = {
    [FOCUS_VISIBLE_SELECTOR]: { outlineOffset: -M3_FOCUS_RING_WIDTH_PX },
};

// The focus ring replaces MUI's pulsing focus ripple, which dims the label while it pulses.
const NO_FOCUS_RIPPLE_PROPS = { disableFocusRipple: true } as const;

// M3 common buttons, like MUI's small fields, are 40 px tall; the M3 touch target is 48 dp, so a
// target reaches 4 px past such a control above and below it.
const M3_BUTTON_HEIGHT_PX = 40;
const M3_TOUCH_TARGET_PX = 48;
const TOUCH_TARGET_OVERHANG_PX = (M3_TOUCH_TARGET_PX - M3_BUTTON_HEIGHT_PX) / 2;

/**
 * A transparent layer that stretches a button's pointer target to 48 px. It reaches no further
 * sideways than the button, so neighbours 8 px apart never share a target.
 */
const BUTTON_TOUCH_TARGET_STYLE: CSSObject = {
    content: '""',
    position: "absolute",
    top: -TOUCH_TARGET_OVERHANG_PX,
    bottom: -TOUCH_TARGET_OVERHANG_PX,
    left: 0,
    right: 0,
};

// The width of the border MUI draws around an outlined button.
const OUTLINED_BORDER_WIDTH_PX = 1;

// The field input styles skip a multiline field's textarea, which keeps MUI's own sizing.
const SINGLE_LINE_INPUT_SELECTOR = "&:not(textarea)";

// A filled field's input padding in grid units around its 24 px line box. The floating label sits
// in the top padding, so the field keeps MUI's 56 px, or 48 px when small; a field with a hidden
// label centres the line box instead, keeping MUI's 56 px, or 40 px when small. As in MUI, a later
// row wins where several match.
const FILLED_INPUT_PADDING_UNITS = { top: 6, bottom: 2 };
const FILLED_INPUT_PADDING_VARIANTS = [
    { props: { size: "small" }, units: { top: 5, bottom: 1 } },
    { props: { hiddenLabel: true }, units: { top: 4, bottom: 4 } },
    { props: { hiddenLabel: true, size: "small" }, units: { top: 2, bottom: 2 } },
] as const;

// The M3 text field's 16 dp inset: the input text, the floating label and the helper text share
// one column 16 px in from either side of an outlined or filled field.
const TEXT_FIELD_INSET_UNITS = 4;

// An adorned side keeps MUI's own spacing: the adornment sits in the field's padding and the input
// beside it has none.
const ADORNED_START_INPUT_SELECTOR = ".MuiInputBase-adornedStart > &";
const ADORNED_END_INPUT_SELECTOR = ".MuiInputBase-adornedEnd > &";

// A floating label is drawn at full size in the field, or shrunk to 0.75 above the input text.
const LABEL_SCALE = { resting: 1, shrunk: 0.75 } as const;

// The InputLabel states that move a floating label.
interface FloatingLabelStateProps {
    size?: "small";
    shrink?: boolean;
}

// One placement of a floating label: MUI's offset from the top of the field, in px, and whether
// the label is shrunk.
interface FloatingLabelPlacement {
    props: FloatingLabelStateProps;
    offsetYPx: number;
    isShrunk: boolean;
}

// MUI's label heights, kept as MUI places them; only the x offset moves to the 16 dp inset. A
// label with no matching row rests at restingOffsetYPx, and, as in MUI, a later row wins where
// several match.
interface FloatingLabelGeometry {
    restingOffsetYPx: number;
    placements: readonly FloatingLabelPlacement[];
}

const OUTLINED_LABEL_GEOMETRY: FloatingLabelGeometry = {
    restingOffsetYPx: 16,
    placements: [
        { props: { size: "small" }, offsetYPx: 9, isShrunk: false },
        { props: { shrink: true }, offsetYPx: -9, isShrunk: true },
    ],
};

const FILLED_LABEL_GEOMETRY: FloatingLabelGeometry = {
    restingOffsetYPx: 16,
    placements: [
        { props: { size: "small" }, offsetYPx: 13, isShrunk: false },
        { props: { shrink: true }, offsetYPx: 7, isShrunk: true },
        { props: { shrink: true, size: "small" }, offsetYPx: 4, isShrunk: true },
    ],
};

// The notch of an outlined field opens 4 dp before its shrunk label and closes 4 dp after it, so
// the notch edge sits 12 px in from the field's edge. The legend that sizes the notch starts
// inside the fieldset's border, which MUI draws 1 px wide, or 2 px while the field has focus.
const NOTCH_GAP_UNITS = 1;
const FIELD_OUTLINE_WIDTH_PX = { resting: 1, focused: 2 } as const;
const FOCUSED_NOTCHED_OUTLINE_SELECTOR = "&.Mui-focused .MuiOutlinedInput-notchedOutline";
// The copy of the label MUI hides in the legend to size the notch. A field without a label gets a
// zero-width placeholder span marked notranslate instead, which stays unpadded so no gap opens.
const NOTCH_LABEL_SELECTOR = "& > legend > span:not(.notranslate)";
// The copy is set at the shrunk label's size but tracks at the full-size bodyLarge letter
// spacing, so it takes the spacing the shrunk label is drawn with and stays as long as the label.
const LABEL_LETTER_SPACING = M3_TYPE_SCALE.bodyLarge.letterSpacing;
const NOTCH_LABEL_LETTER_SPACING = `calc(${LABEL_LETTER_SPACING} * ${LABEL_SCALE.shrunk})`;

// The Select arrow sits 8 px in from the field's edge, inside the 32 px MUI keeps beside the value.
const SELECT_ICON_INSET_UNITS = 2;

// Forced-colours mode drops fills and shadows, so the components that rely on them draw a 1 px
// boundary in a CSS system colour instead, and a selected option takes the Highlight pair.
const FORCED_COLORS_QUERY = "@media (forced-colors: active)";
const FORCED_BOUNDARY_WIDTH_PX = 1;
const SYSTEM_COLORS = {
    buttonText: "ButtonText",
    canvasText: "CanvasText",
    grayText: "GrayText",
    highlight: "Highlight",
    highlightText: "HighlightText",
} as const;
// An enabled selected option, at the specificity of MUI's selected hover and focus rules.
const ENABLED_SELECTED_SELECTOR = "&.Mui-selected:not(.Mui-disabled)";

// The Highlight pair is set as is, so the mode does not recolour the selected option back.
const FORCED_SELECTED_STYLE: CSSObject = {
    forcedColorAdjust: "none",
    backgroundColor: SYSTEM_COLORS.highlight,
    backgroundImage: "none",
    borderColor: SYSTEM_COLORS.highlight,
    color: SYSTEM_COLORS.highlightText,
};

// A Select menu floats over the page; under forced colours its dropped shadow becomes a border.
const FORCED_MENU_PAPER_STYLE: CSSObject = {
    [FORCED_COLORS_QUERY]: { border: ForcedBoundary(SYSTEM_COLORS.canvasText) },
};

// The M3 elevation levels 1 to 5, each a key shadow and an ambient shadow in the shadow role.
const M3_KEY_SHADOW_OPACITY = 0.3;
const M3_AMBIENT_SHADOW_OPACITY = 0.15;
const M3_ELEVATION_LEVELS = [
    { keyShadow: "0px 1px 2px 0px", ambientShadow: "0px 1px 3px 1px" },
    { keyShadow: "0px 1px 2px 0px", ambientShadow: "0px 2px 6px 2px" },
    { keyShadow: "0px 1px 3px 0px", ambientShadow: "0px 4px 8px 3px" },
    { keyShadow: "0px 2px 3px 0px", ambientShadow: "0px 6px 10px 4px" },
    { keyShadow: "0px 4px 4px 0px", ambientShadow: "0px 8px 12px 6px" },
] as const;
// MUI has 25 elevation indices. Index 0 casts no shadow, and each M3 level starts at one index:
// 1-2 render level 1, 3-4 level 2, 5-8 level 3, 9-16 level 4 and 17-24 level 5.
const MUI_ELEVATION_COUNT = 25;
const M3_LEVEL_FIRST_MUI_INDEX = [1, 3, 5, 9, 17] as const;

// The Alert icon fills the first line box of the bodyMedium message beside it, so icon and text
// share the same 8 px inset.
const ALERT_ICON_SIZE = M3_TYPE_SCALE.bodyMedium.lineHeight;

/**
 * White-space handling of the Alert message slot, where server literals are shown: every blank a
 * legacy literal carries stays visible ("must  be", the two blanks after a sentence's full stop),
 * and long lines still wrap at blanks.
 */
export const MESSAGE_TEXT_WHITE_SPACE = "pre-wrap";

// The HTML element each type role renders as when a component gives none.
const M3_VARIANT_MAPPING = {
    displayLarge: "h1",
    displayMedium: "h1",
    displaySmall: "h1",
    headlineLarge: "h2",
    headlineMedium: "h2",
    headlineSmall: "h2",
    titleLarge: "h3",
    titleMedium: "h6",
    titleSmall: "h6",
    bodyLarge: "p",
    bodyMedium: "p",
    bodySmall: "p",
    labelLarge: "span",
    labelMedium: "span",
    labelSmall: "span",
} as const satisfies Record<M3TypeRole, string>;

// The M3 role each built-in MUI variant takes, so MUI's own components render the M3 type scale.
const MUI_VARIANT_ROLES = {
    h1: "displayLarge",
    h2: "displayMedium",
    h3: "displaySmall",
    h4: "headlineMedium",
    h5: "headlineSmall",
    h6: "titleLarge",
    subtitle1: "titleMedium",
    subtitle2: "titleSmall",
    body1: "bodyLarge",
    body2: "bodyMedium",
    caption: "bodySmall",
    overline: "labelSmall",
    button: "labelLarge",
} as const satisfies Record<TypographyVariant, M3TypeRole>;

// The theme MUI hands to style callbacks declared in the theme's own components section.
type StyleTheme = Omit<Theme, "components">;

interface ThemedStyleProps {
    theme: StyleTheme;
}

// The colour roles of one standard Alert severity.
interface AlertColourRoles {
    containerColor: string;
    contentColor: string;
    iconColor: string;
}

type AlertSeverityRoles = Record<"error" | "info", AlertColourRoles>;

// Padding above and below a component's content, in theme spacing units.
interface VerticalPaddingUnits {
    top: number;
    bottom: number;
}

/** A forced-colours boundary: 1 px in the given CSS system colour. */
function ForcedBoundary(systemColor: string): string {
    return `${FORCED_BOUNDARY_WIDTH_PX}px solid ${systemColor}`;
}

/**
 * The touch-target layer of a button with a border. The layer is placed from the padding box,
 * inside the border, so it reaches the border's width further to stay 4 px past the border box
 * above and below, keeping the target 48 px tall.
 */
function BorderedTouchTargetStyle(borderWidthPx: number): CSSObject {
    const insetPx = -(TOUCH_TARGET_OVERHANG_PX + borderWidthPx);
    return { "&::after": { top: insetPx, bottom: insetPx } };
}

/** The M3 elevation level, 0 to 5, that one of MUI's elevation indices renders. */
function M3LevelOf(muiIndex: number): number {
    return M3_LEVEL_FIRST_MUI_INDEX.filter((firstIndex) => muiIndex >= firstIndex).length;
}

/** MUI's 25 shadows, each drawn as the M3 elevation level its index maps to. */
function BuildM3Shadows(): Shadows {
    const keyColor = alpha(M3_COLOR_TOKENS.shadow, M3_KEY_SHADOW_OPACITY);
    const ambientColor = alpha(M3_COLOR_TOKENS.shadow, M3_AMBIENT_SHADOW_OPACITY);
    // Level 0 casts no shadow; M3LevelOf only returns levels this list holds.
    const levelShadows = [
        "none",
        ...M3_ELEVATION_LEVELS.map(
            ({ keyShadow, ambientShadow }) =>
                `${keyShadow} ${keyColor}, ${ambientShadow} ${ambientColor}`,
        ),
    ];
    const shadows = Array.from(
        { length: MUI_ELEVATION_COUNT },
        (_unused, muiIndex) => levelShadows[M3LevelOf(muiIndex)] ?? "none",
    );
    return shadows as Shadows;
}

/**
 * The type roles with the Roboto family on each one: MUI applies typography.allVariants to its
 * built-in variants only, so custom roles carry the family themselves.
 */
function BuildTypographyRoles(): Record<M3TypeRole, CSSProperties> {
    const roleEntries = Object.entries(M3_TYPE_SCALE).map(([role, roleStyle]) => [
        role,
        { fontFamily: FONT_FAMILY, ...roleStyle },
    ]);
    return Object.fromEntries(roleEntries) as Record<M3TypeRole, CSSProperties>;
}

/**
 * MUI's built-in variants in their M3 roles. The button role keeps legacy labels such as
 * "Save (F5)" in their own case, where MUI's button variant upper-cases them.
 */
function BuildBuiltInTypography(): Record<TypographyVariant, TypographyStyle> {
    const variantEntries = Object.entries(MUI_VARIANT_ROLES).map(([variant, role]) => [
        variant,
        { fontFamily: FONT_FAMILY, ...M3_TYPE_SCALE[role] },
    ]);
    const builtInVariants = Object.fromEntries(variantEntries) as Record<
        TypographyVariant,
        TypographyStyle
    >;
    return { ...builtInVariants, button: { ...builtInVariants.button, textTransform: "none" } };
}

function VerticalPadding(theme: StyleTheme, units: VerticalPaddingUnits): CSSObject {
    return { paddingTop: theme.spacing(units.top), paddingBottom: theme.spacing(units.bottom) };
}

/** The M3 hover state layer: on-primary-container at the theme's hover opacity, over the fill. */
function BuildHoverStateLayer(theme: StyleTheme): CSSObject {
    const layerColor = theme.alpha(
        theme.palette.m3.onPrimaryContainer,
        theme.palette.action.hoverOpacity,
    );
    return { backgroundImage: `linear-gradient(${layerColor}, ${layerColor})` };
}

/** The M3 filled-tonal button, which MUI lacks: a primary-container fill and its label colour. */
function BuildTonalButtonStyle({ theme }: ThemedStyleProps): CSSObject {
    return {
        backgroundColor: theme.palette.m3.primaryContainer,
        color: theme.palette.m3.onPrimaryContainer,
        "@media (hover: hover)": {
            "&:hover": { ...BuildHoverStateLayer(theme), boxShadow: theme.shadows[1] },
        },
        "&.Mui-disabled": {
            backgroundColor: theme.palette.action.disabledBackground,
            color: theme.palette.action.disabled,
        },
    };
}

/**
 * A segment that is selected and disabled: the disabled tokens with no state layer, on hover as
 * well, so neither selected rule restores the enabled colours over MUI's disabled text.
 */
function BuildDisabledSegmentStyle(theme: StyleTheme): CSSObject {
    const disabledStyle = {
        backgroundColor: theme.palette.action.disabledBackground,
        backgroundImage: "none",
        color: theme.palette.action.disabled,
    };
    return { ...disabledStyle, "&:hover": disabledStyle };
}

/**
 * One M3 segmented-button segment. Every segment gets the full extra-large radius and
 * ToggleButtonGroup squares the inner corners, so only the first and last segments keep
 * rounded outer corners, in either orientation.
 */
function BuildSegmentStyle({ theme }: ThemedStyleProps): CSSObject {
    const selectedStyle = {
        backgroundColor: theme.palette.m3.primaryContainer,
        color: theme.palette.m3.onPrimaryContainer,
    };
    return {
        borderRadius: M3_SHAPE_SCALE.extraLarge,
        borderColor: theme.palette.m3.outline,
        // The M3 unselected-segment label role, in place of MUI's 54 % black action colour.
        color: theme.palette.text.primary,
        textTransform: "none",
        "&.Mui-selected": {
            ...selectedStyle,
            "&:hover": selectedStyle,
            "@media (hover: hover)": { "&:hover": BuildHoverStateLayer(theme) },
            "&.Mui-disabled": BuildDisabledSegmentStyle(theme),
        },
        [FORCED_COLORS_QUERY]: { [ENABLED_SELECTED_SELECTOR]: FORCED_SELECTED_STYLE },
    };
}

/** A medium segment on the 4 dp grid: 12 px around its 20 px label, 46 px tall with its border. */
function BuildMediumSegmentStyle({ theme }: ThemedStyleProps): CSSObject {
    return { padding: theme.spacing(3) };
}

/** The M3 focus indicator on every MUI button, segment, list item, menu item and icon button. */
function BuildFocusRingStyle({ theme }: ThemedStyleProps): CSSObject {
    return {
        [FOCUS_VISIBLE_SELECTOR]: {
            outline: `${M3_FOCUS_RING_WIDTH_PX}px solid ${theme.palette.secondary.main}`,
            outlineOffset: M3_FOCUS_RING_OFFSET_PX,
            [FORCED_COLORS_QUERY]: { outlineColor: SYSTEM_COLORS.highlight },
        },
    };
}

/** The 16 dp inset beside a single-line input's text, on each side that holds no adornment. */
function TextFieldInsetStyle(theme: StyleTheme): CSSObject {
    const inset = theme.spacing(TEXT_FIELD_INSET_UNITS);
    return {
        paddingLeft: inset,
        paddingRight: inset,
        [ADORNED_START_INPUT_SELECTOR]: { paddingLeft: 0 },
        [ADORNED_END_INPUT_SELECTOR]: { paddingRight: 0 },
    };
}

/**
 * A floating label on the 16 dp inset at one of MUI's heights. It spans the field between the two
 * insets, so a shrunk label's unscaled width is that span over its scale.
 */
function FloatingLabelStyle(theme: StyleTheme, offsetYPx: number, isShrunk: boolean): CSSObject {
    const inset = theme.spacing(TEXT_FIELD_INSET_UNITS);
    const scale = isShrunk ? LABEL_SCALE.shrunk : LABEL_SCALE.resting;
    return {
        transform: `translate(${inset}, ${offsetYPx}px) scale(${scale})`,
        maxWidth: `calc((100% - 2 * ${inset}) / ${scale})`,
    };
}

/** Restates every placement of a label, since this rule outranks each of MUI's own. */
function FloatingLabelGeometryStyle(theme: StyleTheme, geometry: FloatingLabelGeometry): CSSObject {
    const variants = geometry.placements.map(({ props, offsetYPx, isShrunk }) => ({
        props,
        style: FloatingLabelStyle(theme, offsetYPx, isShrunk),
    }));
    return { ...FloatingLabelStyle(theme, geometry.restingOffsetYPx, false), variants };
}

function BuildOutlinedLabelStyle({ theme }: ThemedStyleProps): CSSObject {
    return FloatingLabelGeometryStyle(theme, OUTLINED_LABEL_GEOMETRY);
}

function BuildFilledLabelStyle({ theme }: ThemedStyleProps): CSSObject {
    return FloatingLabelGeometryStyle(theme, FILLED_LABEL_GEOMETRY);
}

/** The fieldset padding that puts the notch edge 12 px in from the field's edge. */
function NotchPaddingStyle(theme: StyleTheme, outlineWidthPx: number): CSSObject {
    const notchEdge = theme.spacing(TEXT_FIELD_INSET_UNITS - NOTCH_GAP_UNITS);
    const notchPadding = `calc(${notchEdge} - ${outlineWidthPx}px)`;
    return { paddingLeft: notchPadding, paddingRight: notchPadding };
}

/**
 * Keeps the outline role on a resting outlined field while a touch device holds a tap's hover,
 * and keeps the notch edge in place while focus widens the outline.
 */
function BuildOutlinedFieldStyle({ theme }: ThemedStyleProps): CSSObject {
    const focusedNotchStyle = NotchPaddingStyle(theme, FIELD_OUTLINE_WIDTH_PX.focused);
    return {
        "@media (hover: none)": {
            [TOUCH_HOVER_RESTING_OUTLINE_SELECTOR]: { borderColor: theme.palette.m3.outline },
        },
        [FOCUSED_NOTCHED_OUTLINE_SELECTOR]: focusedNotchStyle,
    };
}

/** The outline role, with a notch that opens 4 px before the shrunk label and closes 4 px after. */
function BuildNotchedOutlineStyle({ theme }: ThemedStyleProps): CSSObject {
    const labelGap = theme.spacing(NOTCH_GAP_UNITS);
    return {
        borderColor: theme.palette.m3.outline,
        ...NotchPaddingStyle(theme, FIELD_OUTLINE_WIDTH_PX.resting),
        [NOTCH_LABEL_SELECTOR]: {
            paddingLeft: labelGap,
            paddingRight: labelGap,
            letterSpacing: NOTCH_LABEL_LETTER_SPACING,
        },
    };
}

/**
 * The input of an outlined field on the 4 dp grid: a 24 px bodyLarge line box with 16 px above and
 * below keeps MUI's 56 px field, and its text starts on the 16 dp inset. A small field stays 40 px,
 * but its input box spans the 48 dp touch target, overhanging the outline by 4 px above and below,
 * so a tap there reaches the input or the Select itself. A Select keeps MUI's 32 px beside its
 * value for the arrow.
 */
function BuildOutlinedFieldInputStyle({ theme }: ThemedStyleProps): CSSObject {
    const lineBox = M3_TYPE_SCALE.bodyLarge.lineHeight;
    const smallFieldStyle = {
        paddingTop: theme.spacing(3),
        paddingBottom: theme.spacing(3),
        marginTop: -TOUCH_TARGET_OVERHANG_PX,
        marginBottom: -TOUCH_TARGET_OVERHANG_PX,
    };
    return {
        [SINGLE_LINE_INPUT_SELECTOR]: {
            height: lineBox,
            lineHeight: lineBox,
            ...VerticalPadding(theme, { top: 4, bottom: 4 }),
            ...TextFieldInsetStyle(theme),
        },
        variants: [
            { props: { size: "small" }, style: { [SINGLE_LINE_INPUT_SELECTOR]: smallFieldStyle } },
        ],
    };
}

/** The M3 common button: a 40 dp container on the 4 dp grid inside a 48 dp touch target. */
function BuildButtonRootStyle({ theme }: ThemedStyleProps): CSSObject {
    return {
        borderRadius: M3_SHAPE_SCALE.extraLarge,
        // textTransform none keeps legacy labels such as "Save (F5)" in their own case.
        textTransform: "none",
        minHeight: M3_BUTTON_HEIGHT_PX,
        padding: theme.spacing(2, 6),
        "&::after": BUTTON_TOUCH_TARGET_STYLE,
        [FORCED_COLORS_QUERY]: {
            border: ForcedBoundary(SYSTEM_COLORS.buttonText),
            ...BorderedTouchTargetStyle(FORCED_BOUNDARY_WIDTH_PX),
            "&.Mui-disabled": { borderColor: SYSTEM_COLORS.grayText },
        },
    };
}

/** The M3 text button keeps 12 dp beside its label. */
function BuildTextButtonStyle({ theme }: ThemedStyleProps): CSSObject {
    return { padding: theme.spacing(2, 3) };
}

/**
 * M3 menu items are 48 dp tall; from the sm breakpoint MUI shrinks them to their content. Their
 * label sits 8 dp from the top and bottom, a dense item's 4 dp as MUI pads it. Under forced
 * colours the selected option is the Highlight pair, and its HighlightText focus ring keeps the
 * 2 dp offset inside the fill, so the ring does not merge with the fill's edge.
 */
function BuildMenuItemStyle({ theme }: ThemedStyleProps): CSSObject {
    return {
        ...INSET_FOCUS_RING_STYLE,
        minHeight: M3_TOUCH_TARGET_PX,
        ...VerticalPadding(theme, { top: 2, bottom: 2 }),
        [theme.breakpoints.up("sm")]: { minHeight: M3_TOUCH_TARGET_PX },
        [FORCED_COLORS_QUERY]: {
            [ENABLED_SELECTED_SELECTOR]: FORCED_SELECTED_STYLE,
            "&.Mui-selected.Mui-focusVisible": {
                outlineColor: SYSTEM_COLORS.highlightText,
                outlineOffset: -(M3_FOCUS_RING_WIDTH_PX + M3_FOCUS_RING_OFFSET_PX),
            },
        },
        variants: [
            { props: { dense: true }, style: VerticalPadding(theme, { top: 1, bottom: 1 }) },
        ],
    };
}

/** A 24 px icon with 12 px around it fills the 48 dp touch target. */
function BuildIconButtonStyle({ theme }: ThemedStyleProps): CSSObject {
    return { padding: theme.spacing(3) };
}

/**
 * The M3 outlined button border: the outline role at rest; MUI's hover keeps the primary role.
 * The touch target reaches past the border, so it stays 48 px tall.
 */
function BuildOutlinedButtonStyle({ theme }: ThemedStyleProps): CSSObject {
    return {
        "--variant-outlinedBorder": theme.palette.m3.outline,
        ...BorderedTouchTargetStyle(OUTLINED_BORDER_WIDTH_PX),
    };
}

/**
 * The M3 filled text field: the highest surface container with an on-surface-variant active
 * indicator. MUI's hover, focus and error rules keep the on-surface, primary and error indicator.
 */
function BuildFilledFieldStyle({ theme }: ThemedStyleProps): CSSObject {
    const containerColor = theme.palette.m3.surfaceContainerHighest;
    const hoverLayerColor = theme.palette.action.hover;
    return {
        backgroundColor: containerColor,
        "&:hover, &.Mui-focused": { backgroundColor: containerColor },
        "@media (hover: hover)": {
            "&:hover:not(.Mui-disabled)": {
                backgroundImage: `linear-gradient(${hoverLayerColor}, ${hoverLayerColor})`,
            },
        },
        "&.Mui-disabled": {
            backgroundColor: theme.alpha(theme.palette.text.primary, M3_DISABLED_CONTAINER_OPACITY),
        },
        "&::before": { borderBottomColor: theme.palette.text.secondary },
        [FORCED_COLORS_QUERY]: { border: ForcedBoundary(SYSTEM_COLORS.canvasText) },
    };
}

/**
 * The input of a filled field on the 4 dp grid: a 24 px bodyLarge line box below the floating
 * label, its text on the 16 dp inset. This rule outranks MUI's small and hidden-label paddings, so
 * it restates both.
 */
function BuildFilledFieldInputStyle({ theme }: ThemedStyleProps): CSSObject {
    const lineBox = M3_TYPE_SCALE.bodyLarge.lineHeight;
    const variants = FILLED_INPUT_PADDING_VARIANTS.map(({ props, units }) => ({
        props,
        style: { [SINGLE_LINE_INPUT_SELECTOR]: VerticalPadding(theme, units) },
    }));
    return {
        [SINGLE_LINE_INPUT_SELECTOR]: {
            height: lineBox,
            lineHeight: lineBox,
            ...VerticalPadding(theme, FILLED_INPUT_PADDING_UNITS),
            ...TextFieldInsetStyle(theme),
        },
        variants,
    };
}

function BuildTableCellStyle({ theme }: ThemedStyleProps): CSSObject {
    return { borderBottomColor: theme.palette.divider };
}

/**
 * A small cell on the 4 dp grid: 8 px above and below its text. Only a normally padded cell takes
 * it, so MUI's checkbox and unpadded cells keep their own padding.
 */
function BuildSmallTableCellStyle({ theme }: ThemedStyleProps): CSSObject {
    return {
        variants: [{ props: { padding: "normal" }, style: { padding: theme.spacing(2, 4) } }],
    };
}

/** Helper text in the M3 bodySmall role, 4 px below its field. */
function BuildHelperTextStyle({ theme }: ThemedStyleProps): CSSObject {
    return { ...theme.typography.bodySmall, marginTop: theme.spacing(1) };
}

/** An outlined or filled field's helper text starts on the 16 dp inset, under the input text. */
function BuildContainedHelperTextStyle({ theme }: ThemedStyleProps): CSSObject {
    const inset = theme.spacing(TEXT_FIELD_INSET_UNITS);
    return { marginLeft: inset, marginRight: inset };
}

/** The arrow of an outlined or filled Select, 8 px in from the field's edge. */
function BuildSelectIconStyle({ theme }: ThemedStyleProps): CSSObject {
    const iconInset = { right: theme.spacing(SELECT_ICON_INSET_UNITS) };
    return {
        variants: [
            { props: { variant: "outlined" }, style: iconInset },
            { props: { variant: "filled" }, style: iconInset },
        ],
    };
}

/**
 * The M3 container pairs of the standard error and info Alerts, each icon in the matching accent
 * role. Success keeps MUI's green, which the legacy confirmations need and no M3 role provides.
 */
function ReadAlertSeverityRoles({ palette }: StyleTheme): AlertSeverityRoles {
    return {
        error: {
            containerColor: palette.m3.errorContainer,
            contentColor: palette.m3.onErrorContainer,
            iconColor: palette.error.main,
        },
        info: {
            containerColor: palette.m3.primaryContainer,
            contentColor: palette.m3.onPrimaryContainer,
            iconColor: palette.primary.main,
        },
    };
}

function BuildStandardAlertColours(roles: AlertColourRoles): CSSObject {
    return {
        backgroundColor: roles.containerColor,
        color: roles.contentColor,
        "& .MuiAlert-icon": { color: roles.iconColor },
    };
}

/** The message-line Alert on the 4 dp grid; under forced colours a CanvasText boundary. */
function BuildAlertStyle({ theme }: ThemedStyleProps): CSSObject {
    const severityVariants = Object.entries(ReadAlertSeverityRoles(theme)).map(
        ([colorSeverity, roles]) => ({
            props: { variant: "standard", colorSeverity },
            style: BuildStandardAlertColours(roles),
        }),
    );
    return {
        borderRadius: M3_SHAPE_SCALE.small,
        padding: theme.spacing(1, 4),
        [FORCED_COLORS_QUERY]: { border: ForcedBoundary(SYSTEM_COLORS.canvasText) },
        variants: severityVariants,
    };
}

function BuildAlertIconStyle({ theme }: ThemedStyleProps): CSSObject {
    return { padding: theme.spacing(2, 0), fontSize: ALERT_ICON_SIZE };
}

/** The CardDemo MUI theme: M3 colour roles, shape scale, 4 dp spacing and type scale. */
export const CARD_DEMO_THEME = createTheme({
    palette: {
        primary: { main: M3_COLOR_TOKENS.primary, contrastText: M3_COLOR_TOKENS.onPrimary },
        secondary: { main: M3_COLOR_TOKENS.secondary },
        error: { main: M3_COLOR_TOKENS.error },
        background: {
            default: M3_COLOR_TOKENS.surface,
            paper: M3_COLOR_TOKENS.surfaceContainer,
        },
        text: { primary: M3_COLOR_TOKENS.onSurface, secondary: M3_COLOR_TOKENS.onSurfaceVariant },
        divider: M3_COLOR_TOKENS.outlineVariant,
        // Icons in on-surface-variant; state layers and disabled content and containers in
        // on-surface. The selected and activated values stay MUI's.
        action: {
            active: M3_COLOR_TOKENS.onSurfaceVariant,
            hover: alpha(M3_COLOR_TOKENS.onSurface, M3_HOVER_STATE_LAYER_OPACITY),
            hoverOpacity: M3_HOVER_STATE_LAYER_OPACITY,
            focus: alpha(M3_COLOR_TOKENS.onSurface, M3_FOCUS_STATE_LAYER_OPACITY),
            focusOpacity: M3_FOCUS_STATE_LAYER_OPACITY,
            disabled: alpha(M3_COLOR_TOKENS.onSurface, M3_DISABLED_CONTENT_OPACITY),
            disabledOpacity: M3_DISABLED_CONTENT_OPACITY,
            disabledBackground: alpha(
                M3_COLOR_TOKENS.onSurface,
                M3_DISABLED_CONTROL_CONTAINER_OPACITY,
            ),
        },
        m3: {
            primaryContainer: M3_COLOR_TOKENS.primaryContainer,
            onPrimaryContainer: M3_COLOR_TOKENS.onPrimaryContainer,
            tertiary: M3_COLOR_TOKENS.tertiary,
            errorContainer: M3_COLOR_TOKENS.errorContainer,
            onErrorContainer: M3_COLOR_TOKENS.onErrorContainer,
            surfaceContainerHighest: M3_COLOR_TOKENS.surfaceContainerHighest,
            outline: M3_COLOR_TOKENS.outline,
        },
    },
    shape: { borderRadius: M3_SHAPE_SCALE.medium, m3: M3_SHAPE_SCALE },
    shadows: BuildM3Shadows(),
    spacing: SPACING_UNIT_PX,
    typography: { fontFamily: FONT_FAMILY, ...BuildBuiltInTypography(), ...BuildTypographyRoles() },
    components: {
        MuiTypography: { defaultProps: { variantMapping: M3_VARIANT_MAPPING } },
        MuiButtonBase: { styleOverrides: { root: BuildFocusRingStyle } },
        MuiListItemButton: { styleOverrides: { root: INSET_FOCUS_RING_STYLE } },
        MuiMenuItem: { styleOverrides: { root: BuildMenuItemStyle } },
        MuiMenu: { styleOverrides: { paper: FORCED_MENU_PAPER_STYLE } },
        MuiIconButton: {
            defaultProps: NO_FOCUS_RIPPLE_PROPS,
            styleOverrides: { root: INSET_FOCUS_RING_STYLE, sizeMedium: BuildIconButtonStyle },
        },
        MuiButton: {
            defaultProps: NO_FOCUS_RIPPLE_PROPS,
            styleOverrides: {
                root: BuildButtonRootStyle,
                outlined: BuildOutlinedButtonStyle,
                text: BuildTextButtonStyle,
            },
            variants: [{ props: { variant: "tonal" }, style: BuildTonalButtonStyle }],
        },
        MuiToggleButtonGroup: {
            styleOverrides: { root: { borderRadius: M3_SHAPE_SCALE.extraLarge } },
        },
        MuiToggleButton: {
            defaultProps: NO_FOCUS_RIPPLE_PROPS,
            styleOverrides: { root: BuildSegmentStyle, sizeMedium: BuildMediumSegmentStyle },
        },
        MuiTextField: { defaultProps: { variant: "outlined" } },
        MuiOutlinedInput: {
            styleOverrides: {
                root: BuildOutlinedFieldStyle,
                notchedOutline: BuildNotchedOutlineStyle,
                input: BuildOutlinedFieldInputStyle,
            },
        },
        MuiFilledInput: {
            styleOverrides: { root: BuildFilledFieldStyle, input: BuildFilledFieldInputStyle },
        },
        MuiInputLabel: {
            styleOverrides: { outlined: BuildOutlinedLabelStyle, filled: BuildFilledLabelStyle },
        },
        MuiSelect: { styleOverrides: { icon: BuildSelectIconStyle } },
        MuiTableCell: {
            styleOverrides: { root: BuildTableCellStyle, sizeSmall: BuildSmallTableCellStyle },
        },
        MuiFormHelperText: {
            styleOverrides: {
                root: BuildHelperTextStyle,
                contained: BuildContainedHelperTextStyle,
            },
        },
        MuiAlert: {
            styleOverrides: {
                root: BuildAlertStyle,
                icon: BuildAlertIconStyle,
                message: { whiteSpace: MESSAGE_TEXT_WHITE_SPACE },
            },
        },
    },
});

/**
 * Keeps text out of sight but in the accessibility tree, so screen readers still read it: the
 * values of MUI's documented visuallyHidden helper, as an sx style. The 1px sizes and the -1px
 * margin are strings because sx reads a bare 1 as 100% and a bare margin as theme spacing.
 */
export const VISUALLY_HIDDEN_STYLE: SxProps<Theme> = {
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

// Courier, the face the report PDF draws its print lines in, then the platform's fixed-pitch face.
const PRINT_LINE_FONT_FAMILY = '"Courier New", Courier, monospace';

/**
 * A CBTRN03C print line shown as the TRANREPT file prints it, as an sx style: a fixed-pitch face
 * at the M3 body-small size, every blank kept and no wrapping, so the column headings and the
 * 133-column dash rule line up as printed. A line wider than its table scrolls with the table.
 */
export const PRINT_LINE_STYLE: SxProps<Theme> = {
    ...M3_TYPE_SCALE.bodySmall,
    fontFamily: PRINT_LINE_FONT_FAMILY,
    whiteSpace: "pre",
};
