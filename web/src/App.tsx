import { FormEvent, useMemo, useState } from 'react';
import { OperationQueue, OpStatus, ShotOperation } from './queue';
import { useOperations } from './useOperations';

const STATUS_TEXT: Record<OpStatus, string> = {
  queued: '排队中',
  in_flight: '提交中…',
  awaiting_retry: '待重试',
  confirmed: '已确认',
  conflict: '内容冲突',
};

function createBrowserQueue(): OperationQueue {
  const params = new URLSearchParams(window.location.search);
  const autoRetry = params.get('autoretry') !== 'off';
  return new OperationQueue({ autoRetryDelays: autoRetry ? [1000, 2000, 4000] : [] });
}

function latestFailure(ops: ShotOperation[]): ShotOperation | undefined {
  return (
    ops.find((o) => o.status === 'awaiting_retry' && o.error) ??
    ops.find((o) => o.status === 'conflict' && o.error)
  );
}

export default function App({ queue }: { queue?: OperationQueue }) {
  const q = useMemo(() => queue ?? createBrowserQueue(), [queue]);
  const ops = useOperations(q);

  const [sceneId, setSceneId] = useState('');
  const [note, setNote] = useState('');
  const [injectFailure, setInjectFailure] = useState(false);
  const [opIdOverride, setOpIdOverride] = useState('');

  const failed = latestFailure(ops);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!sceneId.trim()) return;
    q.enqueue({
      sceneId: sceneId.trim(),
      note,
      injectFailureAfterCommit: injectFailure,
      clientOpId: opIdOverride.trim() || undefined,
    });
    setNote('');
  }

  return (
    <main className="page">
      <h1>场记镜号发放</h1>
      <p className="hint">
        为场次领取下一条镜号。每个操作携带不可复用的 client_op_id：超时重试、重复提交都会取回同一号码；
        同一标识换内容会被拒绝（409）。
      </p>

      <form onSubmit={onSubmit} className="form">
        <label>
          场次
          <input
            data-testid="scene-input"
            value={sceneId}
            onChange={(e) => setSceneId(e.target.value)}
            placeholder="例如：A-12 夜戏"
            required
          />
        </label>
        <label>
          备注
          <input
            data-testid="note-input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="镜头内容备注"
          />
        </label>
        <button type="submit" data-testid="submit-btn" disabled={!sceneId.trim()}>
          领取下一条镜号
        </button>

        <fieldset className="dev-panel">
          <legend>开发选项</legend>
          <label className="inline">
            <input
              type="checkbox"
              data-testid="inject-failure"
              checked={injectFailure}
              onChange={(e) => setInjectFailure(e.target.checked)}
            />
            注入故障（落库后、回包前返回 503，用于演练重试）
          </label>
          <label className="inline">
            client_op_id 覆盖
            <input
              data-testid="op-id-override"
              value={opIdOverride}
              onChange={(e) => setOpIdOverride(e.target.value)}
              placeholder="留空则自动生成"
            />
          </label>
        </fieldset>
      </form>

      {failed && (
        <div role="alert" data-testid="error-banner" className="banner">
          {failed.error}（操作已保留，可重试）
        </div>
      )}

      <table className="ops">
        <thead>
          <tr>
            <th>镜号</th>
            <th>场次</th>
            <th>备注</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {ops.length === 0 && (
            <tr>
              <td colSpan={5} className="empty">
                暂无操作
              </td>
            </tr>
          )}
          {ops.map((op) => (
            <tr key={op.rowId} data-testid="op-row" className={`status-${op.status}`}>
              <td data-testid="op-number" className="number">
                {op.number ?? '—'}
              </td>
              <td data-testid="op-scene">{op.sceneId}</td>
              <td data-testid="op-note">{op.note}</td>
              <td data-testid="op-status">
                {STATUS_TEXT[op.status]}
                {op.error && <div className="op-error">{op.error}</div>}
              </td>
              <td>
                {op.status === 'awaiting_retry' && (
                  <button data-testid="retry-btn" onClick={() => q.retry(op.rowId)}>
                    重试
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
