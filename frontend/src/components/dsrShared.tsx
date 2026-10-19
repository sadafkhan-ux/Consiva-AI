import { useState, type ReactNode } from "react";
import type { ActiveCaseSummary, ManualVerificationMethod } from "../api/dsr";
import {
  MIN_MANUAL_NOTE,
  formatDate,
  requestTypeLabel,
  REQUEST_TYPE_OPTIONS,
  type SourceStatus,
} from "./dsrLogic";

/**
 * The small panels the New request flow and the All cases view share: the verification code, the
 * code entry box, manual verification, the request-type selector, the duplicate-case warning and
 * the status notices. What they decide on is in dsrLogic.ts.
 */

// ── Small shared components ──────────────────────────────────────────────────────

/** A status message that screen readers announce: errors immediately, the rest politely. */
export function Notice({
  tone,
  children,
}: {
  tone: "info" | "warn" | "error" | "ok";
  children: ReactNode;
}) {
  return (
    <div className={`banner banner-${tone}`} role={tone === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}

/** The case's real id, as the backend returned it, selectable and copyable: the API addresses a
 *  case by this id, not by its reference (DSR-...). */
export function CaseId({ id, reference }: { id: string; reference?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <p className="small muted case-id">
      {reference && <>Reference <span className="mono">{reference}</span> · </>}
      ID <span className="mono" data-testid="case-id">{id}</span>{" "}
      <button type="button" className="linkish" onClick={() => void copy()}>{copied ? "Copied" : "Copy ID"}</button>
    </p>
  );
}

/** Why a button is off, in words on the page -- a tooltip is invisible on a touch screen. */
export function WhyDisabled({ reasons }: { reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul className="why-disabled small muted" aria-label="Why some actions are unavailable">
      {reasons.map((r) => (
        <li key={r}>{r}</li>
      ))}
    </ul>
  );
}

export function SourceWarning({
  status,
  onOpenConfig,
}: {
  status: SourceStatus;
  onOpenConfig?: () => void;
}) {
  if (status !== "none") return null;
  return (
    <Notice tone="warn">
      <strong>No data source is connected yet.</strong> A request can be recorded and tracked here, but
      nothing can be searched until an administrator connects a data source.{" "}
      {onOpenConfig && (
        <button type="button" className="linkish" onClick={onOpenConfig}>
          Open DSR configuration
        </button>
      )}
    </Notice>
  );
}

function expiryText(expiresAt: string): string {
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (Number.isNaN(ms)) return "";
  if (ms <= 0) return "This code has expired.";
  const hours = Math.max(1, Math.round(ms / 3_600_000));
  return `Valid for about ${hours} hour${hours === 1 ? "" : "s"} (until ${new Date(expiresAt).toLocaleString()}).`;
}

/** The code, shown inline and once. It replaces the browser prompt, which blocks the page and
 *  loses the code the moment it is dismissed. */
export function CodePanel({
  challenge,
  expiresAt,
  emailed,
  onDismiss,
}: {
  challenge: string;
  expiresAt: string;
  emailed?: boolean;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(challenge);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <section className="code-panel" aria-label="Verification code">
      <p className="small muted">
        {emailed
          ? "This code was emailed to the requester."
          : "No email was sent. Give this code to the requester yourself."}
      </p>
      <div className="code-row">
        <code className="code-value" data-testid="verification-code">{challenge}</code>
        <button type="button" onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button>
        <button type="button" className="linkish" onClick={onDismiss}>Hide</button>
      </div>
      <p className="small muted">
        {expiryText(expiresAt)} It is shown only once — generate another if it is lost.
      </p>
    </section>
  );
}

/** Enter the requester's code. Off until a code exists, with the reason on the page. */
export function CodeEntry({
  enabled,
  busy,
  onSubmit,
}: {
  enabled: boolean;
  busy: boolean;
  onSubmit: (code: string) => void;
}) {
  const [code, setCode] = useState("");
  return (
    <form
      className="inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (code.trim()) onSubmit(code.trim());
      }}
    >
      <label htmlFor="dsr-code-input">Code from the requester</label>
      <div className="inline-row">
        <input
          id="dsr-code-input"
          value={code}
          autoComplete="off"
          spellCheck={false}
          placeholder="XXXX-XXXX-XXXX"
          disabled={!enabled}
          aria-describedby={enabled ? undefined : "dsr-code-why"}
          onChange={(e) => setCode(e.target.value)}
        />
        <button type="submit" disabled={busy || !enabled || !code.trim()}>Check code</button>
      </div>
      {!enabled && (
        <p id="dsr-code-why" className="small muted">Generate a code first, then enter what the requester sends back.</p>
      )}
    </form>
  );
}

const METHODS: { value: ManualVerificationMethod; label: string }[] = [
  { value: "id_document", label: "ID document checked" },
  { value: "account_login", label: "Verified through their signed-in account" },
  { value: "known_fact_match", label: "Matched against facts only they should know" },
  { value: "in_person", label: "Verified in person" },
  { value: "other", label: "Other (explain in the note)" },
];

/** Manual verification as a form: how, against what, and a note -- all three are recorded
 *  separately on the case's audit trail. */
export function ManualVerifyForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  onSubmit: (v: { method: ManualVerificationMethod; reference: string; note: string }) => void;
  onCancel: () => void;
}) {
  const [method, setMethod] = useState<ManualVerificationMethod | "">("");
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const valid = method !== "" && reference.trim() !== "" && note.trim().length >= MIN_MANUAL_NOTE;
  return (
    <form
      className="inline-form manual-verify"
      aria-label="Verify the requester manually"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && method) onSubmit({ method, reference: reference.trim(), note: note.trim() });
      }}
    >
      <label htmlFor="dsr-mv-method">How were they verified?</label>
      <select id="dsr-mv-method" value={method} onChange={(e) => setMethod(e.target.value as ManualVerificationMethod | "")}>
        <option value="">Choose a method…</option>
        {METHODS.map((m) => (
          <option key={m.value} value={m.value}>{m.label}</option>
        ))}
      </select>
      <label htmlFor="dsr-mv-ref">Reference — what was checked</label>
      <input
        id="dsr-mv-ref"
        value={reference}
        maxLength={200}
        placeholder="Document number, account id, or the facts compared"
        onChange={(e) => setReference(e.target.value)}
      />
      <label htmlFor="dsr-mv-note">What did you check, and why is it enough?</label>
      <textarea id="dsr-mv-note" rows={3} value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
      <div className="inline-row">
        <button type="submit" disabled={busy || !valid} aria-describedby={valid ? undefined : "dsr-mv-why"}>
          Record verification
        </button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
      {!valid && (
        <p id="dsr-mv-why" className="small muted">
          Choose a method, give a reference, and describe what you checked (at least {MIN_MANUAL_NOTE} characters).
          This is recorded on the case with your name.
        </p>
      )}
    </form>
  );
}

/** "What is this request?" -- shown whenever the system could not tell. */
export function TypeSelector({
  busy,
  onApply,
}: {
  busy: boolean;
  onApply: (type: string) => void;
}) {
  const [type, setType] = useState("");
  return (
    <form
      className="inline-form type-selector"
      aria-label="Set the request type"
      onSubmit={(e) => {
        e.preventDefault();
        if (type) onApply(type);
      }}
    >
      <label htmlFor="dsr-type-select">What is this request?</label>
      <div className="inline-row">
        <select id="dsr-type-select" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">Choose a type…</option>
          {REQUEST_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <button type="submit" disabled={busy || !type}>Set request type</button>
      </div>
      <p className="small muted">
        The system could not tell what this request is asking for, so no search has been run. Choose the
        type to continue; identity verification and source authorisation still apply.
      </p>
    </form>
  );
}

/** Another open case exists for this requester. Open it, or say why a second one is needed. */
export function DuplicateNotice({
  existing,
  busy,
  onOpen,
  onProceed,
  onCancel,
}: {
  existing: ActiveCaseSummary[];
  busy: boolean;
  onOpen: (id: string) => void;
  onProceed: (reason: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const valid = reason.trim().length >= 5;
  return (
    <Notice tone="warn">
      <strong>
        {existing.length === 1 ? "This requester already has an open case." : `This requester already has ${existing.length} open cases.`}
      </strong>
      <ul className="duplicate-list">
        {existing.map((c) => (
          <li key={c.id}>
            <span className="mono">{c.reference}</span> — {requestTypeLabel(c.request_type)}, opened {formatDate(c.created_at)}{" "}
            <button type="button" className="linkish" onClick={() => onOpen(c.id)}>Open this case</button>
          </li>
        ))}
      </ul>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) onProceed(reason.trim());
        }}
      >
        <label htmlFor="dsr-dup-reason">Need a separate case anyway? Say why (recorded on the case)</label>
        <div className="inline-row">
          <input id="dsr-dup-reason" value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
          <button type="submit" disabled={busy || !valid}>Open another case</button>
          <button type="button" onClick={onCancel}>Cancel</button>
        </div>
        {!valid && <p className="small muted">Give a reason of at least 5 characters to open another case.</p>}
      </form>
    </Notice>
  );
}
