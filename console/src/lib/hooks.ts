import { useCallback, useEffect, useRef, useState } from 'react';

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/** 마운트/의존성 변경 시 fetch. reload()로 수동 갱신. */
export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fn().then(
      (d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
        }
      },
      (e: Error) => {
        if (!cancelled) {
          setError(e.message);
          setLoading(false);
        }
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, loading, error, reload };
}

/** enabled 동안 ms 간격 폴링. 진행 중 요청과 겹치지 않게 in-flight 가드. */
export function usePolling(fn: () => Promise<void>, ms: number, enabled: boolean): void {
  const inFlight = useRef(false);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const run = async () => {
      if (inFlight.current || cancelled) return;
      inFlight.current = true;
      try {
        await fn();
      } catch {
        /* 폴링 오류는 조용히 무시 — 다음 tick에서 재시도 */
      } finally {
        inFlight.current = false;
      }
    };
    void run();
    const timer = setInterval(run, ms);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled]);
}
