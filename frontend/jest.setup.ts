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

// jsdom implements no Element.scrollIntoView, which MessageLine calls to bring each new outcome
// into view. jsdom lays nothing out, so the stub scrolls nothing; suites can spy on it.
if (typeof Element.prototype.scrollIntoView === "undefined") {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
        writable: true,
        value: () => undefined,
    });
}
