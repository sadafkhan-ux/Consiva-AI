import type { ReactNode } from "react";
import type { PagedState } from "./hooks";
import type { Tone } from "./format";

export function Pill({ tone, children, dashed, testId }: { tone: Tone["tone"]; children: ReactNode; dashed?: boolean; testId?: string }) {
  return (
    <span className={`pill pill-${tone}`} data-testid={testId} style={dashed ? { border: "1px dashed currentColor", background: "transparent" } : undefined}>
      {children}
    </span>
  );
}

export function Skeleton({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} data-testid="skeleton">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} style={{ height: 16, margin: "10px 0", borderRadius: 6, background: "var(--panel-2)", width: `${92 - (i % 3) * 14}%` }} />
      ))}
    </div>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="banner banner-error" role="alert" data-testid="enterprise-error">
      {message}{" "}
      {onRetry && <button type="button" className="secondary small" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty-note" data-testid="empty">{children}</p>;
}

export function Pager<T>({ p, noun = "items" }: { p: PagedState<T>; noun?: string }) {
  const total = p.data?.total ?? 0;
  const from = total === 0 ? 0 : p.offset + 1;
  const to = Math.min(p.offset + p.limit, total);
  return (
    <div className="inline-row" data-testid="pager" style={{ marginTop: 8 }}>
      <button type="button" className="secondary small" disabled={p.offset === 0 || p.loading} onClick={() => p.setOffset(p.offset - p.limit)}>Previous</button>
      <button type="button" className="secondary small" disabled={p.offset + p.limit >= total || p.loading} onClick={() => p.setOffset(p.offset + p.limit)}>Next</button>
      <span className="muted" aria-live="polite">{total === 0 ? `No ${noun}` : `${from} to ${to} of ${total.toLocaleString("en-US")} ${noun}`}</span>
    </div>
  );
}

/** Loading / error / empty / content for a paginated list, in one place so every tab behaves the same. */
export function PagedView<T>({ p, empty, noun, children }: { p: PagedState<T>; empty: ReactNode; noun: string; children: (items: T[]) => ReactNode }) {
  if (p.error && !p.data) return <ErrorNote message={p.error} onRetry={p.reload} />;
  if (!p.data) return <Skeleton />;
  return (
    <div aria-busy={p.loading}>
      {p.error && <ErrorNote message={p.error} onRetry={p.reload} />}
      {p.data.items.length === 0 ? <Empty>{empty}</Empty> : <div className="check-table-wrap">{children(p.data.items)}</div>}
      <Pager p={p} noun={noun} />
    </div>
  );
}
