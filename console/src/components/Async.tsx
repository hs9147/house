import type { ReactNode } from 'react';
import type { AsyncState } from '../lib/hooks';

interface Props<T> {
  state: AsyncState<T>;
  empty?: string;
  children: (data: T) => ReactNode;
}

export default function Async<T>({ state, empty, children }: Props<T>) {
  if (state.loading && state.data === null) {
    return <p className="mutedtext">불러오는 중...</p>;
  }
  if (state.error) return <p className="error">{state.error}</p>;
  if (state.data === null) return null;
  if (Array.isArray(state.data) && state.data.length === 0 && empty) {
    return <p className="mutedtext">{empty}</p>;
  }
  return <>{children(state.data)}</>;
}
