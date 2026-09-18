import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../App';
import { OperationQueue } from '../queue';
import type { AllocateOutcome } from '../api';
import { makeStorage } from './helpers';

const fail503 = (): AllocateOutcome => ({
  ok: false,
  retryable: true,
  kind: 'unavailable',
  message: '503 服务暂时不可用：操作可能已落库，重试将取回同一镜号',
});

describe('App 错误反馈与重试', () => {
  it('失败后保留待重试操作并展示错误，重试后显示镜号', async () => {
    let calls = 0;
    const queue = new OperationQueue({
      allocate: async () => {
        calls += 1;
        return calls === 1 ? fail503() : { ok: true, number: 1, idempotentReplay: true };
      },
      storage: makeStorage(),
      autoRetryDelays: [],
    });
    render(<App queue={queue} />);

    await userEvent.type(screen.getByTestId('scene-input'), 'A-12');
    await userEvent.type(screen.getByTestId('note-input'), '夜戏 追车');
    await userEvent.click(screen.getByTestId('submit-btn'));

    // 失败反馈：横幅 + 行状态“待重试”，镜号未分配
    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveTextContent('503');
    expect(screen.getByTestId('op-status')).toHaveTextContent('待重试');
    expect(screen.getByTestId('op-number')).toHaveTextContent('—');

    // 手动重试后恢复，显示唯一镜号
    await userEvent.click(screen.getByTestId('retry-btn'));
    expect(await screen.findByTestId('op-status')).toHaveTextContent('已确认');
    expect(screen.getByTestId('op-number')).toHaveTextContent('1');
    expect(screen.queryByTestId('error-banner')).not.toBeInTheDocument();
  });

  it('409 冲突时展示冲突反馈且不提供重试', async () => {
    const queue = new OperationQueue({
      allocate: async () => ({
        ok: false,
        retryable: false,
        kind: 'conflict',
        message: '409 冲突：同一 client_op_id 已用于不同内容，请更换操作标识',
      }),
      storage: makeStorage(),
      autoRetryDelays: [],
    });
    render(<App queue={queue} />);

    await userEvent.type(screen.getByTestId('scene-input'), 'B-3');
    await userEvent.click(screen.getByTestId('submit-btn'));

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveTextContent('409');
    expect(screen.getByTestId('op-status')).toHaveTextContent('内容冲突');
    expect(screen.queryByTestId('retry-btn')).not.toBeInTheDocument();
  });
});
