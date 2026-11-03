// Material Design 3 token theme for CardDemo (AAP 0.5); titles per COTTL01Y.
import type { CSSProperties } from "react";
import { createTheme, type CSSObject, type SxProps, type Theme } from "@mui/material/styles";
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
} as const;

/** Material Design 3 corner-radius scale, in px. */
export const M3_SHAPE_SCALE: M3ShapeScale = {
    extraSmall: 4,
    small: 8,
    medium: 12,
    large: 16,
    extraLarge: 28,
};

/** The size, line height and weight of one Material Design 3 type role. */
interface M3TypeRoleStyle {
    fontSize: string;
    lineHeight: string;
    fontWeight: number;
}

/** Material Design 3 type scale: the 15 display, headline, title, body and label roles. */
export const M3_TYPE_SCALE = {
    displayLarge: { fontSize: "57px", lineHeight: "64px", fontWeight: 400 },
    displayMedium: { fontSize: "45px", lineHeight: "52px", fontWeight: 400 },
    displaySmall: { fontSize: "36px", lineHeight: "44px", fontWeight: 400 },
    headlineLarge: { fontSize: "32px", lineHeight: "40px", fontWeight: 400 },
    headlineMedium: { fontSize: "28px", lineHeight: "36px", fontWeight: 400 },
    headlineSmall: { fontSize: "24px", lineHeight: "32px", fontWeight: 400 },
    titleLarge: { fontSize: "22px", lineHeight: "28px", fontWeight: 400 },
    titleMedium: { fontSize: "16px", lineHeight: "24px", fontWeight: 500 },
    titleSmall: { fontSize: "14px", lineHeight: "20px", fontWeight: 500 },
    bodyLarge: { fontSize: "16px", lineHeight: "24px", fontWeight: 400 },
    bodyMedium: { fontSize: "14px", lineHeight: "20px", fontWeight: 400 },
    bodySmall: { fontSize: "12px", lineHeight: "16px", fontWeight: 400 },
    labelLarge: { fontSize: "14px", lineHeight: "20px", fontWeight: 500 },
    labelMedium: { fontSize: "12px", lineHeight: "16px", fontWeight: 500 },
    labelSmall: { fontSize: "11px", lineHeight: "16px", fontWeight: 500 },
} as const satisfies Record<string, M3TypeRoleStyle>;

type M3TypeRole = keyof typeof M3_TYPE_SCALE;

// The Roboto face that layout.tsx loads through next/font and exposes as --font-roboto. The
// var() fallback keeps the declaration valid where the variable is undefined: a bare
// var(--font-roboto) would void the whole stack and drop text to the browser's serif default.
const FONT_FAMILY = "var(--font-roboto, Roboto), Roboto, Helvetica, Arial, sans-serif";

// The 4 dp grid: theme.spacing(4) is 16px.
const SPACING_UNIT_PX = 4;

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

// The theme MUI hands to style callbacks declared in the theme's own components section.
type StyleTheme = Omit<Theme, "components">;

interface ThemedStyleProps {
    theme: StyleTheme;
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
        textTransform: "none",
        "&.Mui-selected": {
            ...selectedStyle,
            "&:hover": selectedStyle,
            "@media (hover: hover)": { "&:hover": BuildHoverStateLayer(theme) },
            "&.Mui-disabled": BuildDisabledSegmentStyle(theme),
        },
    };
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
        m3: {
            primaryContainer: M3_COLOR_TOKENS.primaryContainer,
            onPrimaryContainer: M3_COLOR_TOKENS.onPrimaryContainer,
            tertiary: M3_COLOR_TOKENS.tertiary,
            errorContainer: M3_COLOR_TOKENS.errorContainer,
            outline: M3_COLOR_TOKENS.outline,
        },
    },
    shape: { borderRadius: M3_SHAPE_SCALE.medium, m3: M3_SHAPE_SCALE },
    spacing: SPACING_UNIT_PX,
    typography: { fontFamily: FONT_FAMILY, ...BuildTypographyRoles() },
    components: {
        MuiTypography: { defaultProps: { variantMapping: M3_VARIANT_MAPPING } },
        MuiButton: {
            // textTransform none keeps legacy labels such as "Save (F5)" in their own case.
            styleOverrides: {
                root: { borderRadius: M3_SHAPE_SCALE.extraLarge, textTransform: "none" },
            },
            variants: [{ props: { variant: "tonal" }, style: BuildTonalButtonStyle }],
        },
        MuiToggleButtonGroup: {
            styleOverrides: { root: { borderRadius: M3_SHAPE_SCALE.extraLarge } },
        },
        MuiToggleButton: { styleOverrides: { root: BuildSegmentStyle } },
        MuiTextField: { defaultProps: { variant: "outlined" } },
        MuiAlert: {
            styleOverrides: {
                root: { borderRadius: M3_SHAPE_SCALE.small },
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
