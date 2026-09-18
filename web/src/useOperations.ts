import { useSyncExternalStore } from 'react';
import type { OperationQueue, ShotOperation } from './queue';

export function useOperations(queue: OperationQueue): ShotOperation[] {
  return useSyncExternalStore(
    queue.subscribe,
    () => queue.getOperations(),
  );
}
