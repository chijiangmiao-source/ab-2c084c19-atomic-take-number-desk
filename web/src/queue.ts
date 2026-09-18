/**
 * 待重试操作队列（与框架无关，便于单元测试）。
 *
 * 职责：
 *  - 每个待办操作拥有不可复用的 client_op_id，内容一旦入队不可修改；
 *  - 失败（503/网络错误）后操作保留为“待重试”，并按退避策略自动重试，
 *    也可手动重试；重试携带完全相同的负载，服务端幂等返回同一镜号；
 *  - 操作列表持久化到 localStorage，刷新/崩溃后待重试操作不丢失。
 */
import { allocateShotNumber } from './api';

export type OpStatus = 'queued' | 'in_flight' | 'awaiting_retry' | 'confirmed' | 'conflict';

export interface ShotOperation {
  /** 行标识（UI 内部使用，与服务端无关） */
  rowId: string;
  /** 不可复用的操作标识，服务端以此做幂等 */
  clientOpId: string;
  sceneId: string;
  note: string;
  injectFailureAfterCommit: boolean;
  status: OpStatus;
  number: number | null;
  error: string | null;
  attempts: number;
  createdAt: number;
}

export interface QueueDeps {
  allocate?: typeof allocateShotNumber;
  storage?: Storage | null;
  schedule?: (fn: () => void, ms: number) => void;
  /** 自动重试退避（毫秒）；空数组表示禁用自动重试 */
  autoRetryDelays?: number[];
  now?: () => number;
}

export const STORAGE_KEY = 'shotnum.operations.v1';

export function generateId(prefix: string): string {
  const c = (globalThis as { crypto?: Crypto }).crypto;
  if (c && typeof c.randomUUID === 'function') return `${prefix}-${c.randomUUID()}`;
  return `${prefix}-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

function safeLocalStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export class OperationQueue {
  private ops: ShotOperation[] = [];
  private listeners = new Set<() => void>();
  private readonly allocate: typeof allocateShotNumber;
  private readonly storage: Storage | null;
  private readonly scheduleFn: (fn: () => void, ms: number) => void;
  private readonly autoRetryDelays: number[];
  private readonly now: () => number;

  constructor(deps: QueueDeps = {}) {
    this.allocate = deps.allocate ?? allocateShotNumber;
    this.storage = deps.storage === undefined ? safeLocalStorage() : deps.storage;
    this.scheduleFn = deps.schedule ?? ((fn, ms) => setTimeout(fn, ms));
    this.autoRetryDelays = deps.autoRetryDelays ?? [1000, 2000, 4000];
    this.now = deps.now ?? (() => Date.now());
    this.load();
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  getOperations(): ShotOperation[] {
    return this.ops;
  }

  enqueue(input: {
    sceneId: string;
    note: string;
    injectFailureAfterCommit?: boolean;
    clientOpId?: string;
  }): ShotOperation {
    const op: ShotOperation = {
      rowId: generateId('row'),
      clientOpId: input.clientOpId?.trim() || generateId('op'),
      sceneId: input.sceneId,
      note: input.note,
      injectFailureAfterCommit: input.injectFailureAfterCommit ?? false,
      status: 'queued',
      number: null,
      error: null,
      attempts: 0,
      createdAt: this.now(),
    };
    this.ops = [op, ...this.ops];
    this.persistAndNotify();
    void this.process(op.rowId);
    return op;
  }

  /** 手动重试：携带与首次完全相同的负载。 */
  retry(rowId: string): void {
    void this.process(rowId);
  }

  private find(rowId: string): ShotOperation | undefined {
    return this.ops.find((o) => o.rowId === rowId);
  }

  private update(rowId: string, patch: Partial<ShotOperation>): void {
    this.ops = this.ops.map((o) => (o.rowId === rowId ? { ...o, ...patch } : o));
    this.persistAndNotify();
  }

  private async process(rowId: string): Promise<void> {
    const op = this.find(rowId);
    if (!op || op.status === 'confirmed' || op.status === 'conflict' || op.status === 'in_flight') {
      return;
    }
    this.update(rowId, { status: 'in_flight', error: null });

    const outcome = await this.allocate({
      client_op_id: op.clientOpId,
      scene_id: op.sceneId,
      note: op.note,
      inject_failure_after_commit: op.injectFailureAfterCommit,
    });

    const current = this.find(rowId);
    if (!current) return;

    if (outcome.ok) {
      this.update(rowId, { status: 'confirmed', number: outcome.number, error: null });
      return;
    }
    const attempts = current.attempts + 1;
    if (outcome.kind === 'conflict') {
      // 409 不可重试：同一标识换了内容，必须换新操作标识
      this.update(rowId, { status: 'conflict', error: outcome.message, attempts });
      return;
    }
    this.update(rowId, { status: 'awaiting_retry', error: outcome.message, attempts });
    if (this.autoRetryDelays.length > 0 && attempts <= this.autoRetryDelays.length) {
      const delay = this.autoRetryDelays[Math.min(attempts - 1, this.autoRetryDelays.length - 1)];
      this.scheduleFn(() => this.retry(rowId), delay);
    }
  }

  private persistAndNotify(): void {
    if (this.storage) {
      try {
        this.storage.setItem(STORAGE_KEY, JSON.stringify(this.ops));
      } catch {
        // 存储不可用时仅保留内存态
      }
    }
    this.listeners.forEach((fn) => fn());
  }

  private load(): void {
    if (!this.storage) return;
    let raw: string | null = null;
    try {
      raw = this.storage.getItem(STORAGE_KEY);
    } catch {
      return;
    }
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw) as ShotOperation[];
      if (!Array.isArray(parsed)) return;
      // 上次会话中断在“提交中”的操作，恢复为“待重试”，绝不丢失
      this.ops = parsed.map((o) =>
        o.status === 'in_flight' || o.status === 'queued'
          ? { ...o, status: 'awaiting_retry' as OpStatus }
          : o,
      );
    } catch {
      this.ops = [];
    }
  }
}
