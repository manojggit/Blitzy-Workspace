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
    surface: "#FEF7FF",
    surfaceContainer: "#F3EDF7",
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
// A filled field's container: on-surface at the 6 % MUI derives its own filled container from,
// which over the surface container reads as the M3 highest surface container.
const FILLED_CONTAINER_OPACITY = 0.06;

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

/**
 * A segment's ring is drawn inside it too, as segments abut. ToggleButtonGroup overlaps adjacent
 * segments by 1 px, so the focused one is raised and a selected neighbour's fill never covers it.
 */
const RAISED_SEGMENT_Z_INDEX = 1;
const SEGMENT_FOCUS_RING_STYLE: CSSObject = {
    [FOCUS_VISIBLE_SELECTOR]: {
        outlineOffset: -M3_FOCUS_RING_WIDTH_PX,
        zIndex: RAISED_SEGMENT_Z_INDEX,
    },
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

// The field input styles skip a multiline field's textarea: MUI pads a multiline field from its
// root, around the textarea, so the theme sets that field's padding on the root instead.
const SINGLE_LINE_INPUT_SELECTOR = "&:not(textarea)";

// A filled field's padding in grid units around its 24 px line box, on a single-line input or on
// a multiline field's root. The floating label sits in the top padding, so the field keeps MUI's
// 56 px, or 48 px when small; a field with a hidden label centres the line box instead, keeping
// MUI's 56 px, or 40 px when small. As in MUI, a later row wins where several match.
const FILLED_INPUT_PADDING_UNITS = { top: 6, bottom: 2 };
const FILLED_INPUT_PADDING_VARIANTS = [
    { props: { size: "small" }, units: { top: 5, bottom: 1 } },
    { props: { hiddenLabel: true }, units: { top: 4, bottom: 4 } },
    { props: { hiddenLabel: true, size: "small" }, units: { top: 2, bottom: 2 } },
] as const;

// A multiline field's root padding in grid units around its 24 px line box: a filled field takes
// its single-line input's, and an outlined field keeps MUI's 56 px, or 40 px when small, as a
// single-line outlined field does.
const FILLED_MULTILINE_PADDING: FieldPadding = {
    units: FILLED_INPUT_PADDING_UNITS,
    variants: FILLED_INPUT_PADDING_VARIANTS,
};
const OUTLINED_MULTILINE_PADDING: FieldPadding = {
    units: { top: 4, bottom: 4 },
    variants: [{ props: { size: "small" }, units: { top: 2, bottom: 2 } }],
};
const MULTILINE_FIELD_PROPS = { multiline: true } as const;
// On a multiline field's root the 16 dp inset applies to each side that holds no adornment; an
// adorned side keeps MUI's own spacing, as on a single-line input.
const UNADORNED_START_FIELD_SELECTOR = "&:not(.MuiInputBase-adornedStart)";
const UNADORNED_END_FIELD_SELECTOR = "&:not(.MuiInputBase-adornedEnd)";

// A multiline field rendered as a native textarea, in place of MUI's TextareaAutosize, grows to its
// wrapped value through CSS field sizing, with no hidden copy of the field to measure it.
const NATIVE_TEXTAREA_PROPS = { multiline: true, inputComponent: "textarea" } as const;
const CONTENT_FIELD_SIZING = "content";

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

// One placement of a floating label: MUI's offset from the top of the field, in px, whether the
// label is shrunk, and whether it sits in the outline's notch, above the row the error icon holds.
interface FloatingLabelPlacement {
    props: FloatingLabelStateProps;
    offsetYPx: number;
    isShrunk: boolean;
    isInNotch?: boolean;
}

// Where a floating label is drawn, apart from the states that put it there.
type FloatingLabelPosition = Omit<FloatingLabelPlacement, "props">;

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
        { props: { shrink: true }, offsetYPx: -9, isShrunk: true, isInNotch: true },
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

// The M3 text field marks an error with a trailing error icon as well as the error colour, so the
// state shows without colour and under forced colours. The icon is the 24 px ErrorOutline glyph
// the message line's error Alert shows, 12 px in from the field's end; the field's text keeps its
// 16 dp inset clear of the icon, so in error it ends 52 px in from that end.
const M3_ICON_SIZE_PX = 24;
const ERROR_ICON_INSET_UNITS = 3;
const ERROR_ICON_ROOM_UNITS =
    ERROR_ICON_INSET_UNITS + M3_ICON_SIZE_PX / SPACING_UNIT_PX + TEXT_FIELD_INSET_UNITS;
const ERROR_ICON_PATH = [
    "M11 15h2v2h-2zm0-8h2v6h-2z",
    "m.99-5C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2z",
    "M12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8z",
].join("");
const ERROR_ICON_SVG =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">` +
    `<path d="${ERROR_ICON_PATH}"/></svg>`;
const ERROR_ICON_MASK_IMAGE = `url("data:image/svg+xml,${encodeURIComponent(ERROR_ICON_SVG)}")`;

// The glyph masks the icon's fill, so the fill gives it a colour role, or a system colour under
// forced colours. The -webkit- image serves browsers that read only the prefixed mask.
const ERROR_ICON_MASK_STYLE: CSSObject = {
    maskImage: ERROR_ICON_MASK_IMAGE,
    WebkitMaskImage: ERROR_ICON_MASK_IMAGE,
    maskRepeat: "no-repeat",
    maskPosition: "center",
    maskSize: "100% 100%",
};

// MUI's state class on a field, its label and its input root while the field is in error.
const FIELD_ERROR_SELECTOR = "&.Mui-error";
// Inside a field in error: the input whose own padding keeps the icon's room, which is any
// single-line input with no end adornment after it except a Select's value, and the roots that
// keep the room themselves, around a textarea or an end adornment sized by MUI.
const ERROR_ROOM_INPUT_SELECTOR =
    "&:not(.MuiInputBase-adornedEnd) > .MuiInputBase-input:not(textarea):not(.MuiSelect-select)";
const ERROR_ROOM_ROOT_SELECTOR = "&.MuiInputBase-multiline, &.MuiInputBase-adornedEnd";
// A filled field in error, as its TextField's state reads.
const FILLED_ERROR_PROPS = { variant: "filled", error: true } as const;

// MUI's InputBase learns of browser autofill from the animationstart event of its input: a resting
// input starts the cancel animation, an autofilled one the other, which marks the field filled so
// its label shrinks. MUI tells the two apart by these exact animation names.
const MUI_AUTO_FILL_ANIMATION = "mui-auto-fill";
const MUI_AUTO_FILL_CANCEL_ANIMATION = "mui-auto-fill-cancel";

/**
 * MUI's own autofill animations on every InputBase input, which the outlined, filled and standard
 * inputs build on. The theme restates them because it stops InputBase injecting their keyframes,
 * and MUI drops these animations from any input that injects none.
 */
const AUTOFILL_DETECTION_INPUT_STYLE: CSSObject = {
    animationName: MUI_AUTO_FILL_CANCEL_ANIMATION,
    animationDuration: "10ms",
    "&:-webkit-autofill": {
        animationDuration: "5000s",
        animationName: MUI_AUTO_FILL_ANIMATION,
    },
};

/** Every InputBase input: MUI's autofill detection, and a native multiline textarea's sizing. */
const INPUT_BASE_INPUT_STYLE: CSSObject = {
    ...AUTOFILL_DETECTION_INPUT_STYLE,
    variants: [{ props: NATIVE_TEXTAREA_PROPS, style: { fieldSizing: CONTENT_FIELD_SIZING } }],
};

/**
 * The keyframes of MUI's two autofill animations, for Providers to insert once for the whole app.
 * InputBase would insert its own copy on every mount, and the App Router Emotion cache records
 * every such insert in the browser for good. As in MUI, each keyframe holds one declaration so
 * Emotion's production build keeps it; browsers ignore animation properties inside keyframes, so
 * nothing visibly animates.
 */
export const AUTOFILL_KEYFRAMES_STYLE: CSSObject = {
    [`@keyframes ${MUI_AUTO_FILL_ANIMATION}`]: {
        from: { animationName: MUI_AUTO_FILL_ANIMATION },
    },
    [`@keyframes ${MUI_AUTO_FILL_CANCEL_ANIMATION}`]: {
        from: { animationName: MUI_AUTO_FILL_CANCEL_ANIMATION },
    },
};

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

// A field's padding at rest, and in each state that changes it; a later state wins where several
// match.
interface FieldPadding {
    units: VerticalPaddingUnits;
    variants: readonly { props: object; units: VerticalPaddingUnits }[];
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
 * rounded outer corners, in either orientation. The focus ring follows those corners inside the
 * segment, so it never covers a neighbour or the group's edge.
 */
function BuildSegmentStyle({ theme }: ThemedStyleProps): CSSObject {
    const selectedStyle = {
        backgroundColor: theme.palette.m3.primaryContainer,
        color: theme.palette.m3.onPrimaryContainer,
    };
    return {
        ...SEGMENT_FOCUS_RING_STYLE,
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

/**
 * A medium segment is 40 px tall like the M3 common button, its border included: 8 px above and
 * below its 20 px label and 12 px beside it, on the 4 dp grid.
 */
function BuildMediumSegmentStyle({ theme }: ThemedStyleProps): CSSObject {
    return { minHeight: M3_BUTTON_HEIGHT_PX, padding: theme.spacing(2, 3) };
}

/** The M3 focus ring itself; under forced colours it takes the Highlight system colour. */
function FocusRingStyle(theme: StyleTheme): CSSObject {
    return {
        outline: `${M3_FOCUS_RING_WIDTH_PX}px solid ${theme.palette.secondary.main}`,
        outlineOffset: M3_FOCUS_RING_OFFSET_PX,
        [FORCED_COLORS_QUERY]: { outlineColor: SYSTEM_COLORS.highlight },
    };
}

/** The M3 focus indicator on every MUI button, segment, list item, menu item and icon button. */
function BuildFocusRingStyle({ theme }: ThemedStyleProps): CSSObject {
    return { [FOCUS_VISIBLE_SELECTOR]: FocusRingStyle(theme) };
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
 * A multiline field's root on the 4 dp grid: the 24 px bodyLarge line box that its textarea
 * inherits, the padding a single-line input of the variant has, and the 16 dp inset. A one-row
 * multiline field is then as tall as a single-line one, and its text starts under its label.
 */
function MultilineFieldVariants(theme: StyleTheme, padding: FieldPadding): CSSObject[] {
    const inset = theme.spacing(TEXT_FIELD_INSET_UNITS);
    const fieldStyle = {
        lineHeight: M3_TYPE_SCALE.bodyLarge.lineHeight,
        ...VerticalPadding(theme, padding.units),
        [UNADORNED_START_FIELD_SELECTOR]: { paddingLeft: inset },
        [UNADORNED_END_FIELD_SELECTOR]: { paddingRight: inset },
    };
    const stateVariants = padding.variants.map(({ props, units }) => ({
        props: { ...props, ...MULTILINE_FIELD_PROPS },
        style: VerticalPadding(theme, units),
    }));
    return [{ props: MULTILINE_FIELD_PROPS, style: fieldStyle }, ...stateVariants];
}

/**
 * A floating label on the 16 dp inset at one of MUI's heights. It spans the field between the two
 * insets, so a shrunk label's unscaled width is that span over its scale. In error, a label on the
 * input's row ends clear of the trailing error icon; a label in the notch sits above the icon.
 */
function FloatingLabelStyle(theme: StyleTheme, position: FloatingLabelPosition): CSSObject {
    const inset = theme.spacing(TEXT_FIELD_INSET_UNITS);
    const iconRoom = theme.spacing(ERROR_ICON_ROOM_UNITS);
    const scale = position.isShrunk ? LABEL_SCALE.shrunk : LABEL_SCALE.resting;
    const maxWidth = `calc((100% - 2 * ${inset}) / ${scale})`;
    const errorMaxWidth = `calc((100% - ${inset} - ${iconRoom}) / ${scale})`;
    return {
        transform: `translate(${inset}, ${position.offsetYPx}px) scale(${scale})`,
        maxWidth,
        [FIELD_ERROR_SELECTOR]: { maxWidth: position.isInNotch ? maxWidth : errorMaxWidth },
    };
}

/** Restates every placement of a label, since this rule outranks each of MUI's own. */
function FloatingLabelGeometryStyle(theme: StyleTheme, geometry: FloatingLabelGeometry): CSSObject {
    const variants = geometry.placements.map(({ props, ...position }) => ({
        props,
        style: FloatingLabelStyle(theme, position),
    }));
    const restingPosition = { offsetYPx: geometry.restingOffsetYPx, isShrunk: false };
    return { ...FloatingLabelStyle(theme, restingPosition), variants };
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
 * The error icon's fill: a colour role, and under forced colours a system colour set as is, since
 * the mode would paint any other fill as Canvas and hide the icon.
 */
function ErrorIconColourStyle(colour: string, forcedColour: string): CSSObject {
    return {
        backgroundColor: colour,
        [FORCED_COLORS_QUERY]: { forcedColorAdjust: "none", backgroundColor: forcedColour },
    };
}

/** A disabled field's error icon: disabled content, or GrayText under forced colours. */
function DisabledErrorIconColourStyle(theme: StyleTheme): CSSObject {
    return ErrorIconColourStyle(theme.palette.action.disabled, SYSTEM_COLORS.grayText);
}

/**
 * The M3 trailing error icon, at the given height 12 px in from the field's end, in the error
 * role. It is decorative: the field's aria-invalid and its tie to the message line name the error.
 */
function ErrorIconStyle(theme: StyleTheme, placement: CSSObject): CSSObject {
    return {
        content: '""',
        position: "absolute",
        width: M3_ICON_SIZE_PX,
        height: M3_ICON_SIZE_PX,
        right: theme.spacing(ERROR_ICON_INSET_UNITS),
        pointerEvents: "none",
        ...ERROR_ICON_MASK_STYLE,
        ...ErrorIconColourStyle(theme.palette.error.main, SYSTEM_COLORS.canvasText),
        ...placement,
    };
}

/** The room a field in error keeps at its end for the error icon, inside its root. */
function ErrorIconRoomStyle(theme: StyleTheme): CSSObject {
    const iconRoom = { paddingRight: theme.spacing(ERROR_ICON_ROOM_UNITS) };
    return { [ERROR_ROOM_INPUT_SELECTOR]: iconRoom, [ERROR_ROOM_ROOT_SELECTOR]: iconRoom };
}

/**
 * An outlined field's error icon, centred on its height, on the root, which has no pseudo-elements
 * of its own. On a Select the icon takes the arrow's place, 8 px in from the end, as the M3 text
 * field swaps its trailing icon for the error icon, so the Select keeps its width.
 */
function OutlinedErrorIconStyle(theme: StyleTheme): CSSObject {
    const centred = { top: "50%", transform: "translateY(-50%)" };
    return {
        "&::after": ErrorIconStyle(theme, centred),
        "&.Mui-disabled::after": DisabledErrorIconColourStyle(theme),
        "&.MuiSelect-root::after": { right: theme.spacing(SELECT_ICON_INSET_UNITS) },
        "&.MuiSelect-root .MuiSelect-icon": { display: "none" },
        ...ErrorIconRoomStyle(theme),
    };
}

/**
 * Keeps the outline role on a resting outlined field while a touch device holds a tap's hover,
 * keeps the notch edge in place while focus widens the outline, pads a multiline field, and adds
 * the error icon.
 */
function BuildOutlinedFieldStyle({ theme }: ThemedStyleProps): CSSObject {
    const focusedNotchStyle = NotchPaddingStyle(theme, FIELD_OUTLINE_WIDTH_PX.focused);
    return {
        "@media (hover: none)": {
            [TOUCH_HOVER_RESTING_OUTLINE_SELECTOR]: { borderColor: theme.palette.m3.outline },
        },
        [FOCUSED_NOTCHED_OUTLINE_SELECTOR]: focusedNotchStyle,
        [FIELD_ERROR_SELECTOR]: OutlinedErrorIconStyle(theme),
        variants: MultilineFieldVariants(theme, OUTLINED_MULTILINE_PADDING),
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
 * The M3 filled text field: a 6 % on-surface container with an on-surface-variant active
 * indicator. MUI's hover, focus and error rules keep the on-surface, primary and error indicator.
 * A multiline field is padded as a single-line filled input is.
 */
function BuildFilledFieldStyle({ theme }: ThemedStyleProps): CSSObject {
    const containerColor = theme.alpha(theme.palette.text.primary, FILLED_CONTAINER_OPACITY);
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
        // The TextField around the field draws its error icon (BuildFilledErrorIconStyle).
        [FIELD_ERROR_SELECTOR]: ErrorIconRoomStyle(theme),
        [FORCED_COLORS_QUERY]: { border: ForcedBoundary(SYSTEM_COLORS.canvasText) },
        variants: MultilineFieldVariants(theme, FILLED_MULTILINE_PADDING),
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

/** The icon's top, centring it on a filled input box: its padding around the 24 px line box. */
function FilledErrorIconTop(theme: StyleTheme, units: VerticalPaddingUnits): string {
    return theme.spacing((units.top + units.bottom) / 2);
}

/**
 * A filled field's error icon, 12 px in from its end and centred on its input box at each of its
 * heights. FilledInput draws its active indicator with both pseudo-elements, so the TextField root
 * around it holds the icon. A filled Select keeps its arrow there, so it takes no icon.
 */
function BuildFilledErrorIconStyle({ theme }: ThemedStyleProps): CSSObject {
    const iconTop = { top: FilledErrorIconTop(theme, FILLED_INPUT_PADDING_UNITS) };
    const heightVariants = FILLED_INPUT_PADDING_VARIANTS.map(({ props, units }) => ({
        props: { ...FILLED_ERROR_PROPS, ...props },
        style: { "&::after": { top: FilledErrorIconTop(theme, units) } },
    }));
    const iconStyle = { "&::after": ErrorIconStyle(theme, iconTop) };
    const disabledStyle = { "&::after": DisabledErrorIconColourStyle(theme) };
    return {
        variants: [
            { props: { ...FILLED_ERROR_PROPS, select: false }, style: iconStyle },
            ...heightVariants,
            { props: { ...FILLED_ERROR_PROPS, disabled: true }, style: disabledStyle },
        ],
    };
}

function BuildTableCellStyle({ theme }: ThemedStyleProps): CSSObject {
    return { borderBottomColor: theme.palette.divider };
}

/** The inset beside the text of a small table cell, in theme spacing units: 16 px on each side. */
export const SMALL_TABLE_CELL_INLINE_PADDING_UNITS = 4;

/**
 * A small cell on the 4 dp grid: 8 px above and below its text. Only a normally padded cell takes
 * it, so MUI's checkbox and unpadded cells keep their own padding.
 */
function BuildSmallTableCellStyle({ theme }: ThemedStyleProps): CSSObject {
    const padding = theme.spacing(2, SMALL_TABLE_CELL_INLINE_PADDING_UNITS);
    return { variants: [{ props: { padding: "normal" }, style: { padding } }] };
}

/** A header cell in the M3 titleSmall role, in place of MUI's 24 px medium-weight line. */
function BuildHeadTableCellStyle({ theme }: ThemedStyleProps): CSSObject {
    return { ...theme.typography.titleSmall };
}

/**
 * A total line's cell in the M3 titleSmall role on on-surface, so totals stand out from the
 * bodyMedium detail rows where MUI's footer cell is smaller and in on-surface-variant.
 */
function BuildFooterTableCellStyle({ theme }: ThemedStyleProps): CSSObject {
    return { ...theme.typography.titleSmall, color: theme.palette.text.primary };
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
 * The standard error and info Alerts on their M3 containers. An error is red, as the legacy
 * message line was: its text and icon take the error role. An info Alert pairs the primary
 * container with its on-colour text and a primary icon. Success keeps MUI's green, which the
 * legacy confirmations need and no M3 role provides.
 */
function ReadAlertSeverityRoles({ palette }: StyleTheme): AlertSeverityRoles {
    return {
        error: {
            containerColor: palette.m3.errorContainer,
            contentColor: palette.error.main,
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
        // Disabled field text and labels take the same 38 % on-surface content as disabled buttons.
        text: {
            primary: M3_COLOR_TOKENS.onSurface,
            secondary: M3_COLOR_TOKENS.onSurfaceVariant,
            disabled: alpha(M3_COLOR_TOKENS.onSurface, M3_DISABLED_CONTENT_OPACITY),
        },
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
        MuiInputBase: {
            defaultProps: { disableInjectingGlobalStyles: true },
            styleOverrides: { input: INPUT_BASE_INPUT_STYLE },
        },
        MuiTextField: {
            defaultProps: { variant: "outlined" },
            styleOverrides: { root: BuildFilledErrorIconStyle },
        },
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
            styleOverrides: {
                root: BuildTableCellStyle,
                head: BuildHeadTableCellStyle,
                footer: BuildFooterTableCellStyle,
                sizeSmall: BuildSmallTableCellStyle,
            },
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

// The edge cue of a horizontally scrolling region, in theme spacing units: a shadow 12 px deep at
// each edge, under a 40 px cover in the paper colour whose solid first 12 px hide that shadow.
const SCROLL_SHADOW_WIDTH_UNITS = 3;
const SCROLL_COVER_WIDTH_UNITS = 10;
const SCROLL_COVER_SOLID_STOP = "30%";
const FOCUS_VISIBLE_PSEUDO_SELECTOR = "&:focus-visible";

/**
 * Each edge of a horizontally scrolling region shows a shadow in the shadow role only while content
 * lies beyond it: the paper-coloured covers scroll with the content (attachment local) and hide the
 * shadows fixed to the edges (attachment scroll) wherever the content's own edge is in view. Table
 * cells have no fill, so the cue shows through them.
 */
function ScrollEdgeCueStyle(theme: StyleTheme): CSSObject {
    const cover = theme.palette.background.paper;
    const shadow = alpha(M3_COLOR_TOKENS.shadow, M3_KEY_SHADOW_OPACITY);
    const coverSize = `${theme.spacing(SCROLL_COVER_WIDTH_UNITS)} 100%`;
    const shadowSize = `${theme.spacing(SCROLL_SHADOW_WIDTH_UNITS)} 100%`;
    return {
        backgroundImage: [
            `linear-gradient(to right, ${cover} ${SCROLL_COVER_SOLID_STOP}, transparent)`,
            `linear-gradient(to left, ${cover} ${SCROLL_COVER_SOLID_STOP}, transparent)`,
            `linear-gradient(to right, ${shadow}, transparent)`,
            `linear-gradient(to left, ${shadow}, transparent)`,
        ].join(", "),
        backgroundPosition: "left center, right center, left center, right center",
        backgroundSize: [coverSize, coverSize, shadowSize, shadowSize].join(", "),
        backgroundRepeat: "no-repeat",
        backgroundAttachment: "local, local, scroll, scroll",
    };
}

/**
 * A scrolling region the keyboard reaches as a tab stop, as an sx style: the edge cue on each side
 * that hides content, a thin scrollbar in the outline role where the platform draws one, and the M3
 * focus ring while it holds visible focus.
 */
export const SCROLL_REGION_STYLE: SxProps<Theme> = (theme) => ({
    ...ScrollEdgeCueStyle(theme),
    scrollbarWidth: "thin",
    scrollbarColor: `${theme.palette.m3.outline} transparent`,
    [FOCUS_VISIBLE_PSEUDO_SELECTOR]: FocusRingStyle(theme),
});

// A cell text wider than its fixed column breaks inside it rather than run under the next column.
const TABLE_CELL_SELECTOR = "& .MuiTableCell-root";

/**
 * A table whose columns keep the widths its heading row declares, whatever rows a page holds or
 * when it holds none, as an sx style. It is never narrower than those widths together, given in
 * theme spacing units, so a narrower container scrolls it; a wider one widens every column in
 * proportion.
 */
export function FixedLayoutTableStyle(minWidthUnits: number): SxProps<Theme> {
    return {
        tableLayout: "fixed",
        minWidth: (theme) => theme.spacing(minWidthUnits),
        [TABLE_CELL_SELECTOR]: { overflowWrap: "anywhere" },
    };
}
