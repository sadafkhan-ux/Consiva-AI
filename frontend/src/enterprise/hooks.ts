import { useCallback, useEffect, useRef, useState } from "react";
import type { Page } from "../api/enterprise";
import { enterpriseErrorMessage } from "../api/enterprise";

export interface PagedState<T> {
  data: Page<T> | null;
  loading: boolean;
  error: string | null;
  offset: number;
  limit: number;
  setOffset: (o: number) => void;
  reload: () => void;
}

/**
 * One page of a paginated list. `key` names the filters: when it changes the list goes back to the first page. `refresh` changes when the
 * scan has moved on (the live poll), which re-reads the current page without resetting it. Out-of-order answers are dropped.
 */
export function usePaged<T>(fetcher: (offset: number, limit: number) => Promise<Page<T>>, key: string, limit = 25, refresh = ""): PagedState<T> {
  const [pos, setPos] = useState({ key, offset: 0 });
  const [data, setData] = useState<Page<T> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const offset = pos.key === key ? pos.offset : 0;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchRef
      .current(offset, limit)
      .then((p) => {
        if (!alive) return;
        setData(p);
        setError(null);
      })
      .catch((e) => {
        if (alive) setError(enterpriseErrorMessage(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [key, offset, limit, refresh, nonce]);

  const setOffset = useCallback((o: number) => setPos({ key, offset: Math.max(0, o) }), [key]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, loading, error, offset, limit, setOffset, reload };
}

/** A single value read once (and again when `refresh` changes). */
export function useLoaded<T>(load: () => Promise<T>, key: string, refresh = ""): { data: T | null; loading: boolean; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const ref = useRef(load);
  ref.current = load;
  useEffect(() => {
    let alive = true;
    setLoading(true);
    ref
      .current()
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (alive) setError(enterpriseErrorMessage(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [key, refresh, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, loading, error, reload };
}
