// Jest setup for the CardDemo frontend suites: jest-dom matchers and minimal jsdom gaps
// used by MUI.
import "@testing-library/jest-dom";

// jsdom implements no window.matchMedia, yet MUI useMediaQuery and the responsive layout
// components call it on render. The stub reports that no media query matches.
function CreateMediaQueryListStub(query: string): MediaQueryList {
    return {
        matches: false,
        media: query,
        onchange: null,
        addListener: () => undefined,
        removeListener: () => undefined,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        dispatchEvent: () => false,
    };
}

if (typeof window.matchMedia === "undefined") {
    Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: CreateMediaQueryListStub,
    });
}
