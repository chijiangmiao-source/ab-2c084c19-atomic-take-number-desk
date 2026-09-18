/** 与 FastAPI 发号服务通信的客户端。 */

export interface AllocatePayload {
  client_op_id: string;
  scene_id: string;
  note: string;
  inject_failure_after_commit?: boolean;
}

export type AllocateOutcome =
  | { ok: true; number: number; idempotentReplay: boolean }
  | {
      ok: false;
      retryable: boolean;
      kind: 'conflict' | 'unavailable' | 'network';
      message: string;
    };

export async function allocateShotNumber(
  payload: AllocatePayload,
  fetchImpl: typeof fetch = fetch,
  baseUrl = '',
): Promise<AllocateOutcome> {
  let res: Response;
  try {
    res = await fetchImpl(`${baseUrl}/api/shot-numbers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch {
    return {
      ok: false,
      retryable: true,
      kind: 'network',
      message: '网络错误：无法连接发号服务，操作已保留，可重试',
    };
  }

  if (res.ok) {
    const data = (await res.json()) as { number: number; idempotent_replay: boolean };
    return { ok: true, number: data.number, idempotentReplay: data.idempotent_replay };
  }
  if (res.status === 409) {
    return {
      ok: false,
      retryable: false,
      kind: 'conflict',
      message: '409 冲突：同一 client_op_id 已用于不同内容，请更换操作标识',
    };
  }
  if (res.status === 503) {
    return {
      ok: false,
      retryable: true,
      kind: 'unavailable',
      message: '503 服务暂时不可用：操作可能已落库，重试将取回同一镜号',
    };
  }
  return {
    ok: false,
    retryable: res.status >= 500,
    kind: 'unavailable',
    message: `服务器错误（${res.status}），操作已保留，可重试`,
  };
}
