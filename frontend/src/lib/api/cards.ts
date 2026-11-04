// Mirrors backend/app/schemas/cards.py and backend/app/api/v1/cards.py for COCRDLIC (CCLI),
// COCRDSLC (CCDL) and COCRDUPC (CCUP), BMS maps COCRDLI, COCRDSL and COCRDUP.
//
// A query or request property is optional exactly where its Pydantic field has a default, because
// the server fills it. A response property is required, because the server always serialises every
// field; only the shared ScreenFeedback and PageState keys stay optional, as apiClient.ts defines
// them. Every screen field travels as typed, never trimmed, padded or masked: the server runs the
// filter and key edits, the selection checks, the 7-row paging, the card_ref resolution and the
// CCUP state machine. The CCLI paging literals and the information lines arrive in message and
// info_message of successful responses; filter, selection, key and field edit, not-found, lock,
// record-changed and file-error outcomes reach the caller as a rejected call carrying the legacy
// error body, which a CCLI selection error extends with the redisplayed list and a CCUP outcome
// with its screen, in optional keys as their error models declare them. No type has a card
// verification code, and a full card number appears only where the user keyed it.
import {
    API_CLIENT,
    EncodePathKey,
    type Direction,
    type LegacyErrorResponse,
    type PageState,
    type ReadOptions,
    type ScreenFeedback,
    type UpdateAction,
    type UpdateScreenState,
} from "@/lib/apiClient";

const CARDS_PATH = "/cards";
const SELECTION_PATH = `${CARDS_PATH}/selection`;
const EDIT_SUFFIX = "/edit";

// The server's key-prompt information line (cocrdslc.PROMPT_FOR_KEYS_INFO), verbatim. The CCDL
// and CCUP pages show it on a first entry with no query, which makes no request.
export const PROMPT_FOR_KEYS_INFO = "Please enter Account and Card Number";

// The server reads a list as one repeated parameter per item (selections=&selections=S), so
// array items are sent without axios's default "[]" suffix; blank items are kept.
const REPEATED_PARAMS_SERIALIZER = { indexes: null };

// The GET /cards query (COCRDLI). Without a direction (omitted or null, which axios does not send)
// it is ENTER or the first entry, re-reading the page from startKey; F7 and F8 send the shown
// page's first_key or last_key as startKey, an opaque token rather than a card number.
export interface CardListQuery {
    acctId?: string;
    cardNum?: string;
    startKey?: string;
    direction?: Direction | null;
    page?: number;
    // The CRDSEL1-7 code of every row shown, in row order and blank ("") where none was keyed.
    // The server edits them before paging and never transfers on this call.
    selections?: string[];
}

// The GET /cards/{cardNum} and /cards/{cardNum}/edit query: the ACCTSID field as keyed, and
// isCardRef, which declares the path a card_ref token whatever its length. Without it the server
// takes a path of up to 16 characters for the CARDSID text as keyed.
export interface CardKeyQuery {
    acctId?: string;
    isCardRef?: boolean;
}

// One COCRDLI row: ACCTNOn, the masked CRDNUMn and CRDSTSn, with the card's card_ref token.
export interface CardListRow {
    card_ref: string;
    acct_id: string;
    card_num_masked: string;
    active_status: string;
}

// One COCRDLI page of at most 7 cards in card number order, with its paging position.
export interface CardListResponse extends PageState, ScreenFeedback {
    rows: CardListRow[];
}

// The GET /cards 400: a selection error adds the rows redisplayed from the first card, their page
// keys and page number; a filter edit failure carries the legacy error keys alone.
export interface CardListErrorResponse extends LegacyErrorResponse, PageState {
    rows?: CardListRow[];
}

// One COCRDLI row as keyed: its card_ref token and its raw CRDSELn code.
export interface CardSelectionRow {
    card_ref?: string;
    selection?: string;
}

// The COCRDLI ENTER body: the filters shown, edited before the rows' selection codes.
export interface CardSelectionRequest {
    acct_id?: string;
    card_num?: string;
    rows?: CardSelectionRow[];
}

// The page a valid selection opens: S views (COCRDSLC), U updates (COCRDUPC).
export type CardSelectionAction = "view" | "update";

// Replaces COCRDLIC's XCTL; a refused selection is a rejected call carrying the redisplayed rows.
export interface CardSelectionResponse extends ScreenFeedback {
    action: CardSelectionAction | null;
    card_ref: string;
}

// The POST /cards/selection 400: a selection error adds the redisplayed rows and their page keys
// but no page number; a filter edit failure carries the legacy error keys alone.
export interface CardSelectionErrorResponse extends LegacyErrorResponse {
    rows?: CardListRow[];
    first_key?: string;
    last_key?: string;
}

// The COCRDSL screen: one card read by card number, its number masked.
export interface CardDetailResponse extends ScreenFeedback {
    acct_id: string;
    card_ref: string;
    card_num_masked: string;
    embossed_name: string;
    active_status: string;
    expiry_month: string;
    expiry_year: string;
}

// The COCRDUP fields: ACCTSID, CARDSID, CRDNAME, CRDSTCD, EXPMON, EXPYEAR and the dark EXPDAY.
export interface CardScreenFields {
    acct_id: string;
    card_num: string;
    embossed_name: string;
    active_status: string;
    expiry_month: string;
    expiry_year: string;
    expiry_day: string;
}

// The COCRDUP screen after a fetch, ENTER, F5 or F12. state decides which fields are editable,
// and screen_token is sent back unchanged on the next PUT.
export interface CardScreen extends ScreenFeedback {
    fields: CardScreenFields;
    state: UpdateScreenState;
    screen_token: string;
    card_ref: string;
}

// The 400, 404, 409, 423 and 503 body of the edit fetch and the PUT: the legacy error keys plus
// the screen the outcome sends back, without card_ref. The screen keys are optional, as their
// Pydantic fields have defaults, though every such outcome carries them; none is ever null.
export interface CardScreenErrorResponse extends LegacyErrorResponse {
    fields?: CardScreenFields;
    state?: UpdateScreenState;
    screen_token?: string;
}

// One ENTER (enter), F5 (save) or F12 (cancel) on COCRDUP. The page sends only the inputs its
// state leaves editable; the server reads no other field.
export interface CardUpdateRequest {
    action?: UpdateAction;
    screen_token?: string;
    fields?: Partial<CardScreenFields>;
}

// cardNum is the CARDSID text as keyed or a card_ref token; a card_ref read says so in its query.
function CardPath(cardNum: string): string {
    return `${CARDS_PATH}/${EncodePathKey(cardNum)}`;
}

// The query of a read by a card list selection's card_ref: the server decodes the path as a token
// whatever its length, so an invalid or expired one gives Did not find cards for this search
// condition, never the typed card number's key edit.
function CardRefQuery(acctId: string): CardKeyQuery {
    return { acctId, isCardRef: true };
}

// CCLI (COCRDLIC): ENTER, F7 and F8 on the card list. options carries the owning read's
// AbortSignal, as it does on every read below; the selection and the update are writes and take
// none.
export async function ListCards(
    query: CardListQuery,
    options?: ReadOptions,
): Promise<CardListResponse> {
    const response = await API_CLIENT.get<CardListResponse>(CARDS_PATH, {
        params: query,
        paramsSerializer: REPEATED_PARAMS_SERIALIZER,
        signal: options?.signal,
    });
    return response.data;
}

// CCLI (COCRDLIC): ENTER with selection codes keyed on the list.
export async function SelectCards(request: CardSelectionRequest): Promise<CardSelectionResponse> {
    const response = await API_CLIENT.post<CardSelectionResponse>(SELECTION_PATH, request);
    return response.data;
}

// CCDL (COCRDSLC): ENTER on the card view with both keys as typed.
export async function ViewCard(
    cardNum: string,
    acctId: string,
    options?: ReadOptions,
): Promise<CardDetailResponse> {
    const params: CardKeyQuery = { acctId };
    const response = await API_CLIENT.get<CardDetailResponse>(CardPath(cardNum), {
        params,
        signal: options?.signal,
    });
    return response.data;
}

// CCDL (COCRDSLC): the entry from a list selection, and its ENTER re-reads, by the selection's
// card_ref token and row account.
export async function ViewSelectedCard(
    cardRef: string,
    acctId: string,
    options?: ReadOptions,
): Promise<CardDetailResponse> {
    const response = await API_CLIENT.get<CardDetailResponse>(CardPath(cardRef), {
        params: CardRefQuery(acctId),
        signal: options?.signal,
    });
    return response.data;
}

// CCUP (COCRDUPC): the entry from a list selection, fetching the card into the update screen by
// the selection's card_ref token.
export async function FetchCardForEdit(
    cardRef: string,
    acctId: string,
    options?: ReadOptions,
): Promise<CardScreen> {
    const params = CardRefQuery(acctId);
    const editPath = CardPath(cardRef) + EDIT_SUFFIX;
    const response = await API_CLIENT.get<CardScreen>(editPath, {
        params,
        signal: options?.signal,
    });
    return response.data;
}

// CCUP (COCRDUPC): ENTER, F5 and F12 on the update screen.
export async function UpdateCard(cardNum: string, request: CardUpdateRequest): Promise<CardScreen> {
    const response = await API_CLIENT.put<CardScreen>(CardPath(cardNum), request);
    return response.data;
}
