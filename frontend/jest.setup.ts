// Jest setup for the CardDemo frontend suites: jest-dom matchers and minimal jsdom gaps
// used by MUI and the API wrappers.
import "@testing-library/jest-dom";
import { TextDecoder as NodeTextDecoder } from "node:util";

// jsdom implements no TextDecoder, which every browser has and the CR00 export download decodes
// its legacy error bodies with. Node's implementation of the same WHATWG API fills the gap.
if (typeof globalThis.TextDecoder === "undefined") {
    Object.defineProperty(globalThis, "TextDecoder", {
        writable: true,
        value: NodeTextDecoder,
    });
}

// Suites that select the node environment with a @jest-environment docblock (for example
// sourceReferences.test.ts, which reads files only) have no window or Element, so the DOM
// stubs below are installed only where those globals exist.
const HAS_DOM = typeof window !== "undefined" && typeof Element !== "undefined";

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

if (HAS_DOM && typeof window.matchMedia === "undefined") {
    Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: CreateMediaQueryListStub,
    });
}

// jsdom implements no Element.scrollIntoView, which MessageLine calls to bring each new outcome
// into view. jsdom lays nothing out, so the stub scrolls nothing; suites can spy on it.
if (HAS_DOM && typeof Element.prototype.scrollIntoView === "undefined") {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
        writable: true,
        value: () => undefined,
    });
}
