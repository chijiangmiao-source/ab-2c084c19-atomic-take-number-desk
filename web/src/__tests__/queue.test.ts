import { OperationQueue, STORAGE_KEY, type ShotOperation } from '../queue';
import type { AllocateOutcome } from '../api';
import { makeStorage } from './helpers';

const ok = (number: number): AllocateOutcome => ({ ok: true, number, idempotentReplay: false });
const fail503 = (): AllocateOutcome => ({
  ok: false,
  retryable: true,
  kind: 'unavailable',
  message: '503 服务暂时不可用',
});
const conflict = (): AllocateOutcome => ({
  ok: false,
  retryable: false,
  kind: 'conflict',
  message: '409 冲突',
});

async function settled() {
  // 让队列中的异步 process 走完微任务
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

describe('OperationQueue', () => {
  it('成功后进入已确认并记录镜号', async () => {
    const q = new OperationQueue({ allocate: async () => ok(3), storage: makeStorage(), autoRetryDelays: [] });
    const op = q.enqueue({ sceneId: 'S1', note: '日戏' });
    await settled();
    const cur = q.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(cur.status).toBe('confirmed');
    expect(cur.number).toBe(3);
    expect(cur.error).toBeNull();
  });

  it('可重试失败后保留待重试操作，手动重试取回镜号', async () => {
    let calls = 0;
    const q = new OperationQueue({
      allocate: async () => {
        calls += 1;
        return calls === 1 ? fail503() : ok(1);
      },
      storage: makeStorage(),
      autoRetryDelays: [],
    });
    const op = q.enqueue({ sceneId: 'S1', note: '夜戏' });
    await settled();
    let cur = q.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(cur.status).toBe('awaiting_retry');
    expect(cur.error).toContain('503');
    expect(cur.number).toBeNull();

    q.retry(op.rowId);
    await settled();
    cur = q.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(cur.status).toBe('confirmed');
    expect(cur.number).toBe(1);
  });

  it('自动重试按退避调度，最终恢复', async () => {
    const scheduled: Array<{ fn: () => void; ms: number }> = [];
    let calls = 0;
    const q = new OperationQueue({
      allocate: async () => {
        calls += 1;
        return calls <= 2 ? fail503() : ok(5);
      },
      storage: makeStorage(),
      schedule: (fn, ms) => scheduled.push({ fn, ms }),
      autoRetryDelays: [10, 20],
    });
    const op = q.enqueue({ sceneId: 'S1', note: '' });
    await settled();
    expect(q.getOperations().find((o) => o.rowId === op.rowId)!.status).toBe('awaiting_retry');
    expect(scheduled.map((s) => s.ms)).toEqual([10]);

    scheduled.shift()!.fn();
    await settled();
    expect(scheduled.map((s) => s.ms)).toEqual([20]);

    scheduled.shift()!.fn();
    await settled();
    const cur = q.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(cur.status).toBe('confirmed');
    expect(cur.number).toBe(5);
    expect(calls).toBe(3);
  });

  it('409 冲突不自动重试', async () => {
    const scheduled: unknown[] = [];
    const q = new OperationQueue({
      allocate: async () => conflict(),
      storage: makeStorage(),
      schedule: (fn, ms) => scheduled.push([fn, ms]),
      autoRetryDelays: [10, 20],
    });
    const op = q.enqueue({ sceneId: 'S1', note: 'A' });
    await settled();
    const cur = q.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(cur.status).toBe('conflict');
    expect(cur.error).toContain('409');
    expect(scheduled).toHaveLength(0);
  });

  it('重试携带与首次完全相同的负载', async () => {
    const bodies: unknown[] = [];
    const q = new OperationQueue({
      allocate: async (p) => {
        bodies.push({ ...p });
        return bodies.length === 1 ? fail503() : ok(2);
      },
      storage: makeStorage(),
      autoRetryDelays: [],
    });
    const op = q.enqueue({ sceneId: 'S1', note: '雨戏', injectFailureAfterCommit: true, clientOpId: 'op-fixed-1' });
    await settled();
    q.retry(op.rowId);
    await settled();
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toEqual(bodies[1]);
    expect(bodies[0]).toEqual({
      client_op_id: 'op-fixed-1',
      scene_id: 'S1',
      note: '雨戏',
      inject_failure_after_commit: true,
    });
  });

  it('刷新后待重试操作不丢失（localStorage 持久化）', async () => {
    const storage = makeStorage();
    const q1 = new OperationQueue({ allocate: async () => fail503(), storage, autoRetryDelays: [] });
    const op = q1.enqueue({ sceneId: 'S1', note: '打戏' });
    await settled();

    // 模拟页面重新加载：用同一份 storage 重建队列
    const q2 = new OperationQueue({ allocate: async () => ok(4), storage, autoRetryDelays: [] });
    const restored = q2.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(restored.status).toBe('awaiting_retry');
    expect(restored.clientOpId).toBe(op.clientOpId);

    q2.retry(op.rowId);
    await settled();
    const cur = q2.getOperations().find((o) => o.rowId === op.rowId)!;
    expect(cur.status).toBe('confirmed');
    expect(cur.number).toBe(4);
  });

  it('中断在“提交中”的操作恢复为待重试', () => {
    const storage = makeStorage();
    const stale: ShotOperation = {
      rowId: 'row-1',
      clientOpId: 'op-1',
      sceneId: 'S1',
      note: '',
      injectFailureAfterCommit: false,
      status: 'in_flight',
      number: null,
      error: null,
      attempts: 1,
      createdAt: 1,
    };
    storage.setItem(STORAGE_KEY, JSON.stringify([stale]));
    const q = new OperationQueue({ allocate: async () => ok(1), storage, autoRetryDelays: [] });
    expect(q.getOperations()[0].status).toBe('awaiting_retry');
  });
});
