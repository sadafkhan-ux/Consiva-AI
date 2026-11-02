import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ScanUsage, UsageHistoryItem } from "../api/types";

/**
 * The model usage of one scan, read from the server while the scan runs and once more when it stops.
 *
 * It only reads, in the background and never inside the scan's own polling, so it cannot slow or block a scan: a request that has not come back yet is
 * not repeated, and a failed read leaves the last numbers on screen and shows the error. While `live` it asks every `intervalMs`; when `live` turns
 * false it asks one last time (the final totals) and stops. Another scan's numbers are never shown under this scan's id.
 */
export function useScanUsage(scanId: string | null, live: boolean, intervalMs = 2000): { usage: ScanUsage | null; error: string | null } {
  const [usage, setUsage] = useState<ScanUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const shownFor = useRef<string | null>(null);

  useEffect(() => {
    if (!scanId) {
      shownFor.current = null;
      setUsage(null);
      setError(null);
      return;
    }
    if (shownFor.current !== scanId) {
      shownFor.current = scanId;
      setUsage(null);
      setError(null);
    }
    let cancelled = false;
    let inFlight = false;
    async function pull() {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        const next = await api.getScanUsage(scanId as string);
        if (!cancelled) {
          setUsage(next);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Could not read the usage.");
      } finally {
        inFlight = false;
      }
    }
    void pull();
    const timer = live ? setInterval(pull, intervalMs) : undefined;
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [scanId, live, intervalMs]);

  return { usage: usage && usage.scan_id === scanId ? usage : null, error };
}

const HISTORY_PAGE = 20;

/** The usage of the organisation's scans, newest first, a page at a time. Read again from the newest page whenever `refreshKey` changes (a scan has finished);
 *  `loadMore` appends the next older page, so every scan ever saved can be reached. */
export function useUsageHistory(refreshKey: string): { items: UsageHistoryItem[] | null; error: string | null; hasMore: boolean; loadingMore: boolean; loadMore: () => void } {
  const [items, setItems] = useState<UsageHistoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const count = useRef(0);
  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => api.getUsageHistory(HISTORY_PAGE)).then(   // a read that fails to even start is an error on screen, never a crash
      (rows) => {
        if (!cancelled) {
          count.current = rows.length;
          setItems(rows);
          setHasMore(rows.length === HISTORY_PAGE);
          setError(null);
        }
      },
      (e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Could not read the usage history.");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  function loadMore() {
    if (loadingMore) return;
    setLoadingMore(true);
    Promise.resolve().then(() => api.getUsageHistory(HISTORY_PAGE, count.current)).then(
      (rows) => {
        const seen = new Set<string>();
        setItems((prev) => [...(prev ?? []), ...rows].filter((r) => (seen.has(r.scan_id) ? false : (seen.add(r.scan_id), true))));
        count.current += rows.length;
        setHasMore(rows.length === HISTORY_PAGE);
        setError(null);
      },
      (e) => setError(e instanceof Error ? e.message : "Could not read the usage history."),
    ).finally(() => setLoadingMore(false));
  }
  return { items, error, hasMore, loadingMore, loadMore };
}
