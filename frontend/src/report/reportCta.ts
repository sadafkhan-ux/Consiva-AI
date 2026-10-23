import { CTA_URLS } from "./reportText";

// Which call to action a report shows (scope document, section 8.3). One pure function, so the Free report, the Full report and any test read the same
// rule. The destination carries nothing from the scan: no scan id, domain, evidence or personal data goes into an address.

export type CtaKey = "fix_priority" | "action_plan" | "improve" | "close_gaps" | "monitor" | "expand" | "complete";

export interface CtaInput {
  /** The readiness score, or null when it is withheld. */
  score: number | null;
  /** Assessment coverage, or null when there is none. */
  coverage: number | null;
  /** Checks that apply but could not be tested from outside. */
  unassessed: number;
  /** A confirmed finding rated critical. It outranks a high score. */
  critical: boolean;
  /** An active subscriber is shown the operational action, never the acquisition one. */
  subscriber?: boolean;
}

export interface Cta {
  key: CtaKey;
  /** The button text. */
  label: string;
  /** The card's heading and the sentence under it. */
  heading: string;
  body: string;
  /** The small line under the button. */
  note: string;
  href: string;
}

/** Below this, a high score rests on too little of the site's checks to be called strong. */
export const LOW_COVERAGE = 60;

export function ctaKeyFor(i: CtaInput): CtaKey {
  if (i.critical) return "fix_priority";
  if (i.score === null) return "complete";
  if (i.score >= 90) {
    if (i.coverage === null || i.coverage < LOW_COVERAGE) return "expand";
    if (i.unassessed > 0) return "complete";
    return "monitor";
  }
  if (i.score >= 70) return "close_gaps";
  if (i.score >= 40) return "improve";
  return "action_plan";
}

const FREE: Record<CtaKey, Omit<Cta, "key" | "href">> = {
  fix_priority: { label: "Fix the Highest-Priority Finding", heading: "One finding needs attention first", body: "A confirmed issue outranks the score. Start with it, then work through the rest.", note: "Get a full action plan with legal mapping and expert guidance." },
  action_plan: { label: "Get My DPDP Action Plan", heading: "There is room to improve", body: "The checks we could test show gaps. A plan puts them in the order to fix them.", note: "Get a full scan, detailed findings and expert guidance to close the gaps." },
  improve: { label: "Improve My Score with Consiva AI", heading: "You're making progress", body: "Your website shows some good privacy practices. There are gaps and unverified checks to address for full readiness.", note: "Get a full scan, detailed findings and expert guidance to close the remaining gaps." },
  close_gaps: { label: "Close My Remaining Gaps", heading: "You're on the right track!", body: "Your website shows strong privacy practices in the areas we could test. There are still some gaps and unverified checks to address for full readiness.", note: "Get a full scan, detailed findings and expert guidance to close the remaining gaps." },
  monitor: { label: "Monitor My Website with Consiva AI", heading: "Strong indicators across the checks", body: "Keep it that way: privacy signals change when a site changes.", note: "Scheduled rescans show when something drifts." },
  expand: { label: "Expand My Assessment", heading: "A high score on a small part of the checks", body: "Most applicable checks could not be tested from outside, so read the score with the coverage beside it.", note: "A full scan covers more pages and more of the checks." },
  complete: { label: "Complete My Assessment", heading: "Some checks could not be tested from outside", body: "A website scan cannot see everything. The unassessed checks stay open and are not counted as passed.", note: "Add the document and integration evidence to complete the assessment." },
};

const SUBSCRIBER: Record<CtaKey, string> = {
  fix_priority: "Open Critical Finding",
  action_plan: "Review Priority Findings",
  improve: "Create Remediation Tasks",
  close_gaps: "Run Targeted Verification",
  monitor: "Schedule Next Scan",
  expand: "Expand Scan Scope",
  complete: "Review Unassessed Controls",
};

export function resolveCta(i: CtaInput): Cta {
  const key = ctaKeyFor(i);
  const base = FREE[key];
  return { key, ...base, label: i.subscriber ? SUBSCRIBER[key] : base.label, href: CTA_URLS.improve };
}
