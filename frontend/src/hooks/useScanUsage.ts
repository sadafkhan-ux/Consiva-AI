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

/** The usage of the organisation's recent scans. Read again whenever `refreshKey` changes (a scan has finished). */
export function useUsageHistory(refreshKey: string): { items: UsageHistoryItem[] | null; error: string | null } {
  const [items, setItems] = useState<UsageHistoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => api.getUsageHistory(20)).then(   // a read that fails to even start is an error on screen, never a crash
      (rows) => {
        if (!cancelled) {
          setItems(rows);
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
  return { items, error };
}
