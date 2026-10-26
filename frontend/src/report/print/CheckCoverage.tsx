import type { ScoreRequirementView } from "../dpdpScore";
import "./coverage.css";

const TESTED = new Set(["met", "partially_met", "not_met"]);
const MAX_EVIDENCE = 170;
const short = (t: string) => (t.length > MAX_EVIDENCE ? `${t.slice(0, MAX_EVIDENCE - 1)}…` : t);

/** The evidence of a tested check: a check read from the privacy policy shows each passage it rests on (the contact and the response period, say); any other, its one-line evidence. */
const evidenceOf = (c: ScoreRequirementView): string[] => (c.methodKey === "policy_text" ? c.observations.slice(0, 2) : [c.evidenceLine]);

/** Why a check could not be tested. When the first observation is only a quotation the policy offered (a description of its safeguards), the reason is the observation that follows it. */
const whyOf = (c: ScoreRequirementView): string => (c.observations.length > 1 ? c.observations[c.observations.length - 1] : c.reason || c.evidenceLine);

/**
 * What was tested and what was not: every applicable check, split into those that were tested (by observing the website, or from a passage quoted word for word
 * from the privacy policy) and those that could not be, with the reason and the records that would settle each. Nothing here is a verdict the scan did not
 * reach: a check without enough evidence is neither passed nor failed. Everything is read from the checks the server scored.
 */
export function CheckCoverage({ checks }: { checks: ScoreRequirementView[] }) {
  const applicable = checks.filter((c) => c.statusKey !== "not_applicable");
  const tested = applicable.filter((c) => TESTED.has(c.statusKey));
  const unverified = applicable.filter((c) => c.statusKey === "unable_to_verify");
  const byScan = tested.filter((c) => c.methodKey === "website_scan").length;
  const byPolicy = tested.filter((c) => c.methodKey === "policy_text").length;
  const notApplicable = checks.length - applicable.length;
  return (
    <div className="cc" data-testid="check-coverage">
      <p className="cc-summary" data-testid="cc-summary">
        {applicable.length} applicable checks: {tested.length} tested ({byScan} by observing the website{byPolicy ? `, ${byPolicy} from the privacy policy text` : ""}), {unverified.length} not verified.
      </p>

      <h4>Tested</h4>
      {tested.length === 0 ? <p className="cc-note">No check could be tested.</p> : (
        <table data-testid="cc-tested">
          <thead><tr><th>Check</th><th>How it was tested</th><th>Result</th><th>Evidence</th></tr></thead>
          <tbody>{tested.map((c) => (
            <tr key={c.id}>
              <td><b>{c.id.replace("DPDP-", "")}</b> {c.title}</td>
              <td><span className="cc-how">{c.methodLabel}</span></td>
              <td><span className={`cc-pill cc-${c.tone}`}>{c.verdict}</span></td>
              <td>{evidenceOf(c).map((e) => <div key={e}>{short(e)}</div>)}</td>
            </tr>
          ))}</tbody>
        </table>
      )}

      <h4>Not verified</h4>
      {unverified.length === 0 ? <p className="cc-note">Every applicable check was tested.</p> : (
        <table data-testid="cc-unverified">
          <thead><tr><th>Check</th><th>Why it could not be tested</th><th>What would settle it</th></tr></thead>
          <tbody>{unverified.map((c) => (
            <tr key={c.id}>
              <td><b>{c.id.replace("DPDP-", "")}</b> {c.title}<div className="cc-note">{c.methodKey === "policy_text" ? "Read from the privacy policy text" : c.methodKey === "organisation_records" ? "Needs the organisation's own records" : "Needs a test the scan could not run"}</div></td>
              <td>{short(whyOf(c))}</td>
              <td>{c.neededEvidence.length ? <ul>{c.neededEvidence.map((n) => <li key={n}>{n}</li>)}</ul> : <span className="cc-note">Not recorded.</span>}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
      <p className="cc-note">
        A check that could not be tested is never counted as met or as failed; it stays out of the score and lowers coverage. A statement not found in the privacy
        policy text is not treated as a failure either: the site may publish it somewhere else.{notApplicable ? ` ${notApplicable} further ${notApplicable === 1 ? "check does" : "checks do"} not apply to this organisation.` : ""}
      </p>
    </div>
  );
}
