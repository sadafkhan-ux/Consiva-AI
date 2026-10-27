import type { ScanFailure } from "../api/types";

/** What a person is told when something goes wrong: a few words for a label, one plain sentence, and what they can do next. No technical terms, no raw error text. */
export interface FriendlyError {
  short: string;
  message: string;
  next: string;
}

const E = (short: string, message: string, next: string): FriendlyError => ({ short, message, next });

// The same wording the backend catalogue gives (backend scanner/failure_catalog.py), for errors that arrive as plain text: older scans, analysis steps, and network problems on this page.
// The explanation step (retrieval, the language model, validation of its answer). The scan itself is fine. Recognised BEFORE the website errors, because its messages can mention timeouts too.
const ANALYSIS: [RegExp, FriendlyError][] = [
  [/rag|retriev|embed|vector|knowledge base|nvidia|llm|language model|analysis failed|structured output|output validation|findings? (step|generation)/i, E("Explanations unavailable", "The detailed explanations couldn't be prepared right now.", "Your scan results are still valid. Try running the analysis again in a few minutes.")],
];

const SCANNER: [RegExp, FriendlyError][] = [
  [/robots\.txt.*does not allow|disallow/i, E("Not allowed by the website", "This website doesn't allow automated scanning, so we didn't scan it.", "Ask the website's owner to allow it, or try a page the site permits.")],
  [/certificate has expired|cert_date_invalid|not yet valid|expired certificate/i, E("Website security has expired", "This website's security certificate has expired, so we couldn't open it safely.", "The website's owner needs to renew it. Try again afterwards.")],
  [/certificate|err_cert|err_ssl|self.signed|altnames|issuer/i, E("Website security problem", "This website's security certificate isn't valid, so we couldn't open it safely.", "The website's owner needs to fix it. Try again afterwards.")],
  [/could not read the robots|robots\.txt.*(could not|unreadable|cannot)/i, E("Couldn't check the website's rules", "We couldn't check this website's scanning rules, so we didn't scan it.", "Try again in a few minutes.")],
  [/did not resolve|name_not_resolved|enotfound|getaddrinfo|dns/i, E("Website not found", "We couldn't find this website address.", "Check the spelling of the address and try again.")],
  [/scan (rate limit|quota) reached|already has \d+ scans? waiting/i, E("Scan limit reached", "You've reached the number of scans allowed for now.", "Wait a little while and try again, or ask your administrator to raise the limit.")],
  [/\b429\b|too many requests|rate.?limit/i, E("Website is limiting us", "The website is limiting how fast we can scan it.", "Wait a few minutes and try again.")],
  [/\b401\b|authentication|log ?in required|unauthori[sz]ed/i, E("Needs a login", "This page needs a login, which we can't use.", "Scan a public page of the site instead.")],
  [/\b(403|451)\b|access denied|refusing the scanner|forbidden/i, E("Blocked by the website", "The website blocked our scanner.", "If it's your site, allow the scanner in its firewall. Otherwise ask the owner for permission.")],
  [/closed the connection|refused|reset|err_connection|socket hang up|econn|http2_protocol/i, E("Couldn't connect", "The website refused or dropped our connection.", "Check that the site is online. If it's yours, allow our scanner, then try again.")],
  [/\b5\d\d\b|server error|bad gateway|temporarily unavailable/i, E("Website had a problem", "The website had a problem and couldn't load.", "Try again later.")],
  [/exceeded its .*(budget|time)|time limit|was aborted|aborted so the worker/i, E("Scan took too long", "The scan took longer than allowed and was stopped.", "Try again. Large or slow websites sometimes need a second attempt.")],
  [/timeout|timed out|too slow/i, E("Website too slow", "The website took too long to respond.", "Try again in a few minutes.")],
];

const SERVICE: [RegExp, FriendlyError][] = [
  [/the service did not answer|failed to fetch|networkerror|network error|load failed|connection to the backend|lost its connection|status 0|<\s*(html|body)/i, E("Service unavailable", "We couldn't reach the service just now.", "Wait a minute and refresh the page. Your scan may still be running or finished.")],
  [/scan was cancelled|cancelled/i, E("Scan stopped", "The scan was stopped before it finished.", "Start a new scan whenever you're ready.")],
  [/not (been )?authori[sz]ed|authori[sz]ation|ownership|verif(y|ication)/i, E("Confirmation needed", "We need confirmation that you're allowed to scan this website.", "Tick the confirmation box, or verify the domain, then try again.")],
  [/invalid url|not a valid|could not parse|unsupported (url|scheme)|private|loopback|internal address|ssrf/i, E("Address can't be scanned", "That address can't be scanned.", "Enter a public website address, for example https://example.com.")],
  // The explanation step (retrieval, the language model, validation of its answer): the scan itself is fine.
  [/\brag\b|retriev|embed|vector|knowledge base|nvidia|\bllm\b|language model|model|prompt|token|validation|structured output|analysis failed|findings? (step|generation)|analy[sz]e/i, E("Explanations unavailable", "The detailed explanations couldn't be prepared right now.", "Your scan results are still valid. Try running the analysis again in a few minutes.")],
];

const UNKNOWN = E("Something went wrong", "Something went wrong while scanning.", "Please try again. If it keeps happening, contact support and quote the scan ID.");

/** The message a person should see for a raw error (and, for a scan the server has already described, its own plain message). The raw text is written to the browser console for developers, never shown. */
export function friendlyError(raw: string | null | undefined, failure?: ScanFailure | null): FriendlyError {
  if (failure?.user_message) {
    return { short: failure.short || "Scan failed", message: failure.user_message, next: failure.what_you_can_do || UNKNOWN.next };
  }
  const text = (raw ?? "").trim();
  if (!text) return UNKNOWN;
  if (typeof console !== "undefined") console.warn("[error shown to the user in plain words]", text.slice(0, 500));
  const found = [...SERVICE.slice(0, 2), ...ANALYSIS, ...SCANNER, ...SERVICE.slice(2)].find(([pattern]) => pattern.test(text));
  return found ? found[1] : UNKNOWN;
}
