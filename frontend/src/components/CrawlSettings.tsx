import { useState } from "react";
import type { CrawlConfig } from "../api/types";
import { CRAWL_FIELDS, validateCrawlConfig, type CrawlForm } from "../report/crawlConfig";

/**
 * Settings for a whole-site scan, with the button that starts one. Whole-site scans are for organisation administrators on domains that have passed DNS ownership
 * verification: the server enforces both and refuses otherwise, and clamps every value here to its own ceiling. This form only checks what was typed.
 */
export function CrawlSettings({ disabled, onStart }: { disabled: boolean; onStart: (crawl: CrawlConfig) => void }) {
  const [form, setForm] = useState<CrawlForm>({});
  const check = validateCrawlConfig(form);
  return (
    <details className="crawl-settings" data-testid="crawl-settings" style={{ margin: "8px 0" }}>
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>Whole-site scan (verified domains, administrators)</summary>
      <p className="small muted" style={{ margin: "6px 0" }}>Crawls every page it can reach, within the limits below. The server decides what is allowed and may lower any value. The standard scan above is unchanged.</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
        {CRAWL_FIELDS.map((f) => (
          <label key={f.key} className="small" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span>{f.label}</span>
            <input inputMode="decimal" aria-label={f.label} aria-invalid={!!check.errors[f.key]} value={form[f.key] ?? ""} disabled={disabled}
              onChange={(e) => setForm((s) => ({ ...s, [f.key]: e.target.value }))} data-testid={`crawl-${f.key}`} />
            <span className="muted">{f.hint}</span>
            {check.errors[f.key] && <span role="alert" style={{ color: "var(--bad, #b32630)" }} data-testid={`crawl-error-${f.key}`}>{check.errors[f.key]}</span>}
          </label>
        ))}
      </div>
      <button type="button" disabled={disabled || !check.ok} onClick={() => onStart(check.value)} data-testid="crawl-start" style={{ marginTop: 10 }}>Scan whole site</button>
    </details>
  );
}
