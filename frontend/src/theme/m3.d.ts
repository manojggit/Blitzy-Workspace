// Types the Material Design 3 tokens that app/theme.ts defines (AAP 0.5.3, 0.5.4).
import type { CSSProperties } from "react";
import "@mui/material/styles";
import "@mui/material/Typography";
import "@mui/material/Button";

export interface M3PaletteTokens {
    primaryContainer: string;
    onPrimaryContainer: string;
    tertiary: string;
    errorContainer: string;
    onErrorContainer: string;
    surfaceContainerHighest: string;
    outline: string;
}

export interface M3ShapeScale {
    extraSmall: number;
    small: number;
    medium: number;
    large: number;
    extraLarge: number;
}

declare module "@mui/material/styles" {
    interface Palette {
        m3: M3PaletteTokens;
    }

    interface PaletteOptions {
        m3?: Partial<M3PaletteTokens>;
    }

    interface Shape {
        m3: M3ShapeScale;
    }

    interface ShapeOptions {
        m3?: Partial<M3ShapeScale>;
    }

    interface TypographyVariants {
        displayLarge: CSSProperties;
        displayMedium: CSSProperties;
        displaySmall: CSSProperties;
        headlineLarge: CSSProperties;
        headlineMedium: CSSProperties;
        headlineSmall: CSSProperties;
        titleLarge: CSSProperties;
        titleMedium: CSSProperties;
        titleSmall: CSSProperties;
        bodyLarge: CSSProperties;
        bodyMedium: CSSProperties;
        bodySmall: CSSProperties;
        labelLarge: CSSProperties;
        labelMedium: CSSProperties;
        labelSmall: CSSProperties;
    }

    interface TypographyVariantsOptions {
        displayLarge?: CSSProperties;
        displayMedium?: CSSProperties;
        displaySmall?: CSSProperties;
        headlineLarge?: CSSProperties;
        headlineMedium?: CSSProperties;
        headlineSmall?: CSSProperties;
        titleLarge?: CSSProperties;
        titleMedium?: CSSProperties;
        titleSmall?: CSSProperties;
        bodyLarge?: CSSProperties;
        bodyMedium?: CSSProperties;
        bodySmall?: CSSProperties;
        labelLarge?: CSSProperties;
        labelMedium?: CSSProperties;
        labelSmall?: CSSProperties;
    }
}

declare module "@mui/material/Typography" {
    interface TypographyPropsVariantOverrides {
        displayLarge: true;
        displayMedium: true;
        displaySmall: true;
        headlineLarge: true;
        headlineMedium: true;
        headlineSmall: true;
        titleLarge: true;
        titleMedium: true;
        titleSmall: true;
        bodyLarge: true;
        bodyMedium: true;
        bodySmall: true;
        labelLarge: true;
        labelMedium: true;
        labelSmall: true;
    }
}

declare module "@mui/material/Button" {
    interface ButtonPropsVariantOverrides {
        tonal: true;
    }
}
