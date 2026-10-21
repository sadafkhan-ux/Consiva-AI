import { useState } from "react";
import type { DomainReport, DomainRow } from "./subdomains";
import { RiskBadge } from "./shared";

const EXTERNAL_ROWS_SHOWN = 15;

function RequestCounts({ r }: { r: DomainRow }) {
  const { pre_consent: pre, post_accept: acc, post_reject: rej } = r.requests;
  const c = r.cookies;
  const requests = pre + acc + rej === 0 ? "no requests seen" : `Requests — before consent ${pre} · after Accept ${acc} · after Reject ${rej}`;
  const cookies = c.pre_consent + c.post_accept + c.post_reject === 0
    ? ""
    : ` · Cookies set — before consent ${c.pre_consent} · after Accept ${c.post_accept} · after Reject ${c.post_reject} (${c.names.join(", ")})`;
  return <span>{requests}{cookies}</span>;
}

function Row({ r, mainHost }: { r: DomainRow; mainHost: string }) {
  return (
    <div className="domain-row" data-testid={`domain-${r.key}`}>
      <div className="finding-head">
        <strong className="mono">{r.key}</strong>
        <span className="badge neutral">{r.kind === "subdomain" ? "subdomain" : "separate website"}</span>
        {r.framed && <span className="badge neutral">shown in a frame</span>}
        {r.needsReview && <span className="badge warn">review</span>}
      </div>
      <div className="muted small"><RequestCounts r={r} /></div>
      {r.namedVendors.length > 0 && (
        <div className="muted small">Named analytics or advertising: {r.namedVendors.join(", ")}</div>
      )}
      {r.unclassifiedRequests > 0 && (
        <div className="muted small">
          {r.unclassifiedRequests} request{r.unclassifiedRequests === 1 ? "" : "s"} before consent could not be classified: purpose unknown.
        </div>
      )}
      {r.linksFound > 0 && (
        <div className="muted small">
          Linked {r.linksFound} time{r.linksFound === 1 ? "" : "s"}, not followed{r.sampleUrl ? <> — e.g. <span className="mono">{r.sampleUrl}</span></> : null}.
          {r.kind === "subdomain" && <> Enter it as the address to scan it.</>}
        </div>
      )}
      {r.findings.map((f) => (
        <div key={f.id} className="muted small" style={{ marginTop: 4 }}>
          <RiskBadge level={f.risk} /> {f.text}
          <div>{f.fromDomain} of {f.total} evidence items for this finding involve {r.key}, requested by {mainHost}.</div>
        </div>
      ))}
    </div>
  );
}

/**
 * Subdomains of the entered site and external sites that the scan came across. Shown apart, and quietly highlighted,
 * because none of them is a page that was scanned: the scan covers the entered site only. They are here because the
 * entered site's pages called them, linked to them or displayed them -- something to review, not a verdict.
 */
export function DomainsReviewSection({ report }: { report: DomainReport }) {
  const [showAll, setShowAll] = useState(false);
  if (report.subdomains.length === 0 && report.external.length === 0) return null;

  const external = showAll ? report.external : report.external.slice(0, EXTERNAL_ROWS_SHOWN);
  return (
    <section className="section domain-review" data-testid="domains-review">
      <h2>Discovered Subdomains &amp; Websites</h2>
      <p className="muted small">
        The scan covers {report.mainHost} only. The subdomains and separate websites below were found while scanning it,
        through links on its pages, requests its pages made, or content they display. They were not crawled and are not
        pages of {report.mainHost}; their requests and cookies are counted here, not in the site&apos;s own trackers and
        cookies, and findings that involve them are marked as such. Everything here is for review, not a verdict.
      </p>

      {report.subdomains.length > 0 && (
        <>
          <h3>Subdomains of {report.mainHost} ({report.subdomains.length})</h3>
          {report.subdomains.map((r) => <Row key={r.key} r={r} mainHost={report.mainHost} />)}
        </>
      )}

      {report.external.length > 0 && (
        <>
          <h3>Separate websites ({report.external.length})</h3>
          {external.map((r) => <Row key={r.key} r={r} mainHost={report.mainHost} />)}
          {report.external.length > EXTERNAL_ROWS_SHOWN && (
            <button type="button" className="secondary small" onClick={() => setShowAll((v) => !v)} data-testid="domains-toggle">
              {showAll ? "Show fewer" : `Show all ${report.external.length}`}
            </button>
          )}
        </>
      )}
    </section>
  );
}
