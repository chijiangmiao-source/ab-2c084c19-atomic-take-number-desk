import { allocateShotNumber, type AllocatePayload } from '../api';

const payload: AllocatePayload = {
  client_op_id: 'op-test-1',
  scene_id: 'A-1',
  note: '测试',
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('allocateShotNumber', () => {
  it('200 时返回镜号', async () => {
    const fetchImpl = async () =>
      jsonResponse(200, {
        client_op_id: payload.client_op_id,
        scene_id: payload.scene_id,
        note: payload.note,
        number: 7,
        idempotent_replay: false,
      });
    const res = await allocateShotNumber(payload, fetchImpl as typeof fetch);
    expect(res).toEqual({ ok: true, number: 7, idempotentReplay: false });
  });

  it('409 归类为不可重试的冲突', async () => {
    const fetchImpl = async () => jsonResponse(409, { detail: { error: 'client_op_id_conflict' } });
    const res = await allocateShotNumber(payload, fetchImpl as typeof fetch);
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.kind).toBe('conflict');
      expect(res.retryable).toBe(false);
      expect(res.message).toContain('409');
    }
  });

  it('503 归类为可重试', async () => {
    const fetchImpl = async () => jsonResponse(503, { detail: 'injected failure after commit' });
    const res = await allocateShotNumber(payload, fetchImpl as typeof fetch);
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.kind).toBe('unavailable');
      expect(res.retryable).toBe(true);
      expect(res.message).toContain('503');
    }
  });

  it('网络异常归类为可重试', async () => {
    const fetchImpl = async () => {
      throw new TypeError('fetch failed');
    };
    const res = await allocateShotNumber(payload, fetchImpl as unknown as typeof fetch);
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.kind).toBe('network');
      expect(res.retryable).toBe(true);
    }
  });

  it('提交的内容与负载一致（含注入故障标志）', async () => {
    let seenBody = '';
    const fetchImpl = async (_url: unknown, init?: RequestInit) => {
      seenBody = String(init?.body ?? '');
      return jsonResponse(200, { number: 1, idempotent_replay: false });
    };
    await allocateShotNumber(
      { ...payload, inject_failure_after_commit: true },
      fetchImpl as unknown as typeof fetch,
    );
    const parsed = JSON.parse(seenBody);
    expect(parsed).toEqual({
      client_op_id: 'op-test-1',
      scene_id: 'A-1',
      note: '测试',
      inject_failure_after_commit: true,
    });
  });
});
