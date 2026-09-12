/* ---------------------------------------------------------------------------
   One HTTP transport and one error decoder (review 2026-09-05, F14 · T17/T22).

   THE DEFECT this module exists to remove: `api.ts` repeated the same
   fetch/decode block for GET, POST, PUT, PATCH and DELETE, and each copy
   understood exactly one error shape -- FastAPI's `{"detail": ...}`. The
   backend's own middleware (`src/aida/main.py`, `request_context`) emits
   `{"error": {"code", "message", "correlation_id"}}` and sets an
   `X-Correlation-Id` response header on *every* response. All of that was
   discarded and replaced with `"500 Internal Server Error"`, so a user
   reporting a failure had nothing to give support, and support had nothing to
   grep the logs with.

   WHAT AN ERROR MUST CARRY:
     - a message that is safe to show a user,
     - the machine-readable `code` when the server sent one,
     - the correlation id, from the body or the header, so a screen can print
       "quote this reference" and it will actually find the request,
     - per-field errors from 422 validation, so a form can mark its own inputs,
     - whether retrying is meaningful at all.

   RETRY POLICY: `retryable` describes the *server's* answer, not permission to
   retry automatically. Nothing in this module retries. A 409 on a governance
   decision means someone else decided first (see F05) -- resubmitting it
   silently would be the bug, not the fix. Automatic retry of a non-idempotent
   write is never correct; the caller decides, with a button the user presses.
--------------------------------------------------------------------------- */

export interface FieldError {
  /** Dotted path into the request body, e.g. `body.name`. */
  readonly field: string;
  readonly message: string;
}

export interface ApiErrorInit {
  readonly code?: string | null;
  readonly correlationId?: string | null;
  readonly fieldErrors?: readonly FieldError[];
  readonly retryable?: boolean;
  readonly details?: Readonly<Record<string, unknown>> | null;
}

/**
 * A failed API call.
 *
 * `detail` is the human-readable message and is kept as the second
 * constructor argument for the call sites and tests that already build one
 * directly (`new ApiError(403, "policy_denied")`).
 */
export class ApiError extends Error {
  readonly code: string | null;
  readonly correlationId: string | null;
  readonly fieldErrors: readonly FieldError[];
  readonly retryable: boolean;
  /**
   * A structured `detail` object, when the server sent one instead of a string.
   *
   * THE DEFECT this removes: `messageFromDetail` recognised a string and a
   * validation array and returned nothing for an object, so an object-shaped
   * `detail` was dropped whole -- message and payload alike -- and the caller
   * was left with the bare status line. The governance decision service
   * (F05) answers a lost claim with exactly that shape:
   * `{"message", "outcome", "review": {status, decided_by, decided_at,
   * decision_reason}}`. Discarding it meant the reviewer who lost the race was
   * told "409 Conflict" while the server had already said who decided, what
   * they decided, and why.
   *
   * Kept as raw `unknown` values: this is the wire body, and a consumer that
   * wants a field narrows it at the point of use.
   */
  readonly details: Readonly<Record<string, unknown>> | null;

  constructor(
    readonly status: number,
    readonly detail: string,
    init: ApiErrorInit = {},
  ) {
    super(detail);
    this.name = "ApiError";
    this.code = init.code ?? null;
    this.correlationId = init.correlationId ?? null;
    this.fieldErrors = init.fieldErrors ?? [];
    this.retryable = init.retryable ?? defaultRetryable(status);
    this.details = init.details ?? null;
  }

  /** True when the server refused because someone else changed the record. */
  get isConflict(): boolean {
    return this.status === 409;
  }

  /** True when the caller is not authenticated (as opposed to not permitted). */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** True when the caller is authenticated but not permitted. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** A one-line reference a user can paste into a support request. */
  get supportReference(): string | null {
    return this.correlationId;
  }
}

/**
 * Whether retrying the *same* request could plausibly succeed.
 *
 * 408/429 and 5xx are transient-by-contract. 502/503/504 are the proxy and
 * dependency failures this app actually sees. Everything else is the caller's
 * request being wrong, and repeating it will be wrong again.
 */
function defaultRetryable(status: number): boolean {
  if (status === 408 || status === 429) return true;
  return status >= 500 && status !== 501;
}

interface StructuredErrorBody {
  error?: { code?: unknown; message?: unknown; correlation_id?: unknown };
  detail?: unknown;
}

function messageFromDetail(value: unknown): {
  message: string | null;
  fields: FieldError[];
  details?: Record<string, unknown>;
} {
  if (typeof value === "string") return { message: value, fields: [] };
  if (Array.isArray(value)) {
    // FastAPI request validation: [{loc: ["body","name"], msg: "...", ...}]
    const fields: FieldError[] = [];
    for (const item of value) {
      if (!item || typeof item !== "object") continue;
      const record = item as { loc?: unknown; msg?: unknown };
      const field =
        Array.isArray(record.loc) && record.loc.length > 0 ? record.loc.join(".") : "request";
      const message = typeof record.msg === "string" ? record.msg : "is not valid";
      fields.push({ field, message });
    }
    if (fields.length === 0) return { message: null, fields };
    return {
      message: fields.map((f) => `${f.field}: ${f.message}`).join("; "),
      fields,
    };
  }
  if (value && typeof value === "object") {
    // A structured refusal. `message` is the sentence the server already
    // intends a person to read (`_ConflictDetail.__str__` in
    // `semantic_api.py` returns exactly this key); the rest of the object is
    // kept verbatim for a caller that understands the outcome it names.
    const record = value as Record<string, unknown>;
    const message = typeof record.message === "string" && record.message ? record.message : null;
    return { message, fields: [], details: record };
  }
  return { message: null, fields: [] };
}

/**
 * Turn a non-2xx `Response` into an `ApiError`, preserving everything the
 * server told us.
 *
 * Never throws on a malformed body: a failure to parse the error must not
 * replace the failure the user is actually looking at.
 */
export async function decodeError(res: Response): Promise<ApiError> {
  const headerCorrelationId = res.headers.get("X-Correlation-Id");
  let message = `${res.status} ${res.statusText}`.trim();
  let code: string | null = null;
  let correlationId: string | null = headerCorrelationId;
  let fieldErrors: FieldError[] = [];
  let details: Record<string, unknown> | null = null;

  try {
    const body = (await res.json()) as StructuredErrorBody;
    if (body && typeof body === "object") {
      const structured = body.error;
      if (structured && typeof structured === "object") {
        if (typeof structured.message === "string" && structured.message) {
          message = structured.message;
        }
        if (typeof structured.code === "string" && structured.code) code = structured.code;
        if (typeof structured.correlation_id === "string" && structured.correlation_id) {
          correlationId = structured.correlation_id;
        }
      }
      if (body.detail !== undefined) {
        const decoded = messageFromDetail(body.detail);
        if (decoded.message) message = decoded.message;
        fieldErrors = decoded.fields;
        details = decoded.details ?? null;
      }
    }
  } catch {
    /* Non-JSON error body (an HTML proxy page, an empty 502). The status line
       plus the correlation header is what we have, and it is still useful. */
  }

  return new ApiError(res.status, message, { code, correlationId, fieldErrors, details });
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  readonly signal?: AbortSignal;
  /** Extra headers merged over the defaults (identity, accept, content type). */
  readonly headers?: Record<string, string>;
  /** JSON request body. Omit for GET/DELETE. */
  readonly body?: unknown;
}

/** Supplies per-request headers (identity, organization). Set by `api.ts`. */
export type HeaderProvider = () => Record<string, string>;

let headerProvider: HeaderProvider = () => ({});

/**
 * Install the identity/organization header provider.
 *
 * The transport must not import the React layer or the org provider -- that
 * was the `api -> org -> api` cycle the review called out (F14/R03). The
 * composition root injects it instead.
 */
export function setHeaderProvider(provider: HeaderProvider): void {
  headerProvider = provider;
}

/** Observers notified of every completed request. Powers the session state. */
export type RequestObserver = (outcome: {
  readonly ok: boolean;
  readonly status: number;
  readonly at: number;
  readonly error?: ApiError;
}) => void;

const observers = new Set<RequestObserver>();

/** Watch request outcomes. Returns an unsubscribe function. */
export function observeRequests(observer: RequestObserver): () => void {
  observers.add(observer);
  return () => {
    observers.delete(observer);
  };
}

function notify(outcome: Parameters<RequestObserver>[0]): void {
  for (const observer of observers) observer(outcome);
}

/**
 * One request, one decoder, for every verb.
 *
 * The response is cast to `T`. That cast is not validation: `types.ts` is
 * generated from the server's own OpenAPI schema and drift is caught by the
 * `ui-types-diff` CI gate, which is a stronger guarantee than a hand-written
 * runtime check that would itself drift. Boundaries that need real runtime
 * validation should validate explicitly at the call site.
 */
export async function request<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const hasBody = options.body !== undefined;
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(hasBody ? { "Content-Type": "application/json" } : {}),
    ...headerProvider(),
    ...options.headers,
  };

  let res: Response;
  try {
    res = await fetch(path, {
      method,
      signal: options.signal,
      headers,
      credentials: "same-origin",
      ...(hasBody ? { body: JSON.stringify(options.body) } : {}),
    });
  } catch (cause) {
    // An aborted request is the caller's own doing, not a transport failure,
    // and must not be reported as the app losing its connection.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    const error = new ApiError(0, "the server could not be reached", {
      code: "NETWORK_UNREACHABLE",
      retryable: true,
    });
    notify({ ok: false, status: 0, at: Date.now(), error });
    throw error;
  }

  if (!res.ok) {
    const error = await decodeError(res);
    notify({ ok: false, status: res.status, at: Date.now(), error });
    throw error;
  }

  notify({ ok: true, status: res.status, at: Date.now() });
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/**
 * A download that must carry this app's identity headers.
 *
 * A bare `<a download href>` cannot send them, so the bytes are fetched here
 * and handed back with the response for the caller to read
 * `Content-Disposition` from. Errors decode exactly as they do for JSON.
 */
export async function requestBlob(
  path: string,
  options: RequestOptions = {},
): Promise<{ blob: Blob; response: Response }> {
  const headers: Record<string, string> = {
    Accept: "*/*",
    ...headerProvider(),
    ...options.headers,
  };
  let res: Response;
  try {
    res = await fetch(path, {
      signal: options.signal,
      headers,
      credentials: "same-origin",
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    const error = new ApiError(0, "the server could not be reached", {
      code: "NETWORK_UNREACHABLE",
      retryable: true,
    });
    notify({ ok: false, status: 0, at: Date.now(), error });
    throw error;
  }
  if (!res.ok) {
    const error = await decodeError(res);
    notify({ ok: false, status: res.status, at: Date.now(), error });
    throw error;
  }
  notify({ ok: true, status: res.status, at: Date.now() });
  return { blob: await res.blob(), response: res };
}

/** A sentence for a page that did not load, for rendering beside the list.
 *
 * Every paged screen had the same bare `catch {}`: the list stopped growing
 * and said nothing, so "you have reached the end" and "you were refused"
 * looked identical. That is worst exactly where it matters most -- on an audit
 * ledger a silent stop turns a truncated record into an apparently complete
 * one -- and R11-B11's browser journey hit it on every least-privilege
 * identity, where a refusal is the ordinary case rather than the exceptional
 * one.
 *
 * A 403 is named as a refusal rather than a failure, because the reader's next
 * action differs: ask for access, not retry. The server's own `detail` is
 * preferred over anything invented here whenever it sent one.
 */
export function describeLoadMoreFailure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return error.detail
        ? `More results were refused: ${error.detail}`
        : "More results were refused — you may not have access to the rest of this list.";
    }
    return error.detail
      ? `More results could not be loaded: ${error.detail}`
      : `More results could not be loaded (HTTP ${error.status}).`;
  }
  return "More results could not be loaded. What is shown above is incomplete.";
}
