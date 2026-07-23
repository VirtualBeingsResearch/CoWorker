import { useEffect, useState } from 'react';
import * as api from '../api/client';
import { useBranchState } from '../hooks/useBranchState';
import type { Branch } from '../api/types';
import { TranscriptView } from './TranscriptView';

interface Props {
  branch: Branch;
  onForked: (newBranchId: string) => void;
  onBranchPatched: () => void;
}

function parseJsonMaybe(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function renderJson(value: unknown): string {
  try {
    return JSON.stringify(parseJsonMaybe(value), null, 2);
  } catch {
    return String(value);
  }
}

export function BranchDetail({ branch, onForked, onBranchPatched }: Props) {
  const [runtimeBranch, setRuntimeBranch] = useState(branch);
  const [waking, setWaking] = useState(false);
  const [wakeError, setWakeError] = useState<string | null>(null);
  const [wakeStartedAt, setWakeStartedAt] = useState<number | null>(null);
  const [wakeElapsedSeconds, setWakeElapsedSeconds] = useState(0);
  const isAwake = runtimeBranch.pid !== null && runtimeBranch.status !== 'stopped' && runtimeBranch.status !== 'crashed';
  const { state, error, refresh } = useBranchState(isAwake && !waking ? runtimeBranch.control_port : null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [overrideText, setOverrideText] = useState(state?.system_prompt_override_text ?? '');
  const [stepN, setStepN] = useState(3);
  const [label, setLabel] = useState(branch.label);
  const [note, setNote] = useState(branch.note);
  const [forkLabel, setForkLabel] = useState('');
  const [virtualConnectionsText, setVirtualConnectionsText] = useState('');

  useEffect(() => {
    setRuntimeBranch(branch);
    setWakeError(null);
    setWakeStartedAt(null);
    setWakeElapsedSeconds(0);
  }, [branch.id]);

  useEffect(() => {
    if (!waking || wakeStartedAt === null) return;

    const updateElapsed = () => {
      setWakeElapsedSeconds(Math.max(0, Math.floor((Date.now() - wakeStartedAt) / 1000)));
    };
    const pollBranch = () => {
      api
        .getBranch(branch.id)
        .then(next => setRuntimeBranch(next))
        .catch(() => {
          // The wake request will surface the real error; polling is only for progress hints.
        });
    };

    updateElapsed();
    pollBranch();
    const elapsedTimer = setInterval(updateElapsed, 1000);
    const pollTimer = setInterval(pollBranch, 1500);
    return () => {
      clearInterval(elapsedTimer);
      clearInterval(pollTimer);
    };
  }, [branch.id, wakeStartedAt, waking]);

  const wake = async () => {
    setWaking(true);
    setWakeError(null);
    setActionError(null);
    setWakeStartedAt(Date.now());
    setWakeElapsedSeconds(0);
    try {
      const next = await api.wakeBranch(branch.id);
      setRuntimeBranch(next);
    } catch (e) {
      setWakeError(e instanceof Error ? e.message : '分支唤醒失败');
    } finally {
      setWaking(false);
      setWakeStartedAt(null);
    }
  };

  useEffect(() => {
    if (state?.virtual_connections) {
      setVirtualConnectionsText(state.virtual_connections.join('\n'));
    }
  }, [state?.virtual_connections?.join('\n')]);

  const run = async (fn: () => Promise<unknown>, refreshState = false) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      if (refreshState) await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const controlPort = runtimeBranch.control_port;
  const status = waking ? 'starting' : state?.status ?? runtimeBranch.status;
  const canStep = status === 'paused';
  const canPause = status === 'running';
  const controlsDisabled = busy || waking || !!wakeError || !state;
  const subconsciousEnabled = state?.subconscious_enabled ?? false;
  const wakePhase =
    runtimeBranch.pid === null
      ? '准备工作目录与端口'
      : runtimeBranch.status === 'starting'
        ? `进程已启动 pid=${runtimeBranch.pid}，等待 runtime ready`
        : `当前状态：${runtimeBranch.status}`;

  return (
    <div className="branch-detail">
      <header>
        <h2>{branch.label || branch.id}</h2>
        <span className={`status-pill status-${status}`}>{status}</span>
        {state?.auto_paused_reason && <span className="hint">（自动暂停：{state.auto_paused_reason}）</span>}
        {(!isAwake || waking) && (
          <button disabled={waking} onClick={wake}>
            {waking ? '唤醒中...' : '唤醒'}
          </button>
        )}
      </header>

      {waking && (
        <section className="wake-progress" aria-label="分支唤醒进度">
          <div className="wake-progress-head">
            <strong>{wakePhase}</strong>
            <span>{wakeElapsedSeconds}s</span>
          </div>
          <div className="wake-progress-bar" role="progressbar" aria-valuetext={wakePhase}>
            <span />
          </div>
          <div className="wake-progress-meta">
            <span>port: {runtimeBranch.pid === null ? '-' : runtimeBranch.control_port}</span>
            <span>status: {runtimeBranch.status}</span>
          </div>
        </section>
      )}

      {error && <p className="error-text">状态拉取失败：{error}</p>}
      {wakeError && <p className="error-text">唤醒失败：{wakeError}</p>}
      {actionError && <p className="error-text">{actionError}</p>}
      {state?.last_error && (
        <p className="error-text">
          上次出错：{state.last_error.type} — {state.last_error.message}
        </p>
      )}

      <section className="control-bar">
        <button disabled={!canStep || controlsDisabled} onClick={() => run(() => api.step(controlPort), true)}>
          step
        </button>
        <input
          type="number"
          min={1}
          value={stepN}
          onChange={e => setStepN(Number(e.target.value))}
          style={{ width: 48 }}
        />
        <button
          disabled={!canStep || controlsDisabled}
          onClick={() => run(() => api.stepN(controlPort, stepN, 'until_reply'), true)}
        >
          step_n（跑到回复为止）
        </button>
        <button disabled={!canStep || controlsDisabled} onClick={() => run(() => api.backStep(controlPort), true)}>
          back_step
        </button>
        <button disabled={!canPause || controlsDisabled} onClick={() => run(() => api.pause(controlPort), true)}>
          pause
        </button>
        <button disabled={!canStep || controlsDisabled} onClick={() => run(() => api.resume(controlPort), true)}>
          resume
        </button>
        <span className="hint">undo 栈深度：{state?.undo_depth ?? '-'}</span>
        <span className="hint">cycle：{state?.cycle_count ?? '-'}</span>
        <span className="hint">
          模型：{state?.current_provider}/{state?.current_model}
        </span>
        <label className="inline-toggle">
          <input
            type="checkbox"
            checked={subconsciousEnabled}
            disabled={controlsDisabled}
            onChange={e => run(() => api.setSubconsciousEnabled(controlPort, e.target.checked), true)}
          />
          潜意识
        </label>
        <span className="hint">潜意识 pending：{state?.subconscious_pending?.length ?? 0}</span>
      </section>

      <section className="input-bar">
        <PushInputForm controlPort={controlPort} disabled={controlsDisabled} onError={setActionError} />
      </section>

      <section className="two-col">
        <div>
          <h3>Transcript</h3>
          <TranscriptView messages={state?.transcript ?? []} />
        </div>
        <div>
          <h3>System Prompt Override</h3>
          <textarea
            value={overrideText ?? ''}
            onChange={e => setOverrideText(e.target.value)}
            rows={6}
            placeholder="留空 = 不覆盖，用真实 prompt_builder.build()"
          />
          <div className="row-buttons">
            <button
              disabled={controlsDisabled}
              onClick={() =>
                run(async () => {
                  const prompt = await api.getSystemPrompt(controlPort);
                  setOverrideText(prompt.base_text);
                })
              }
            >
              加载真实 prompt
            </button>
            <button
              disabled={controlsDisabled}
              onClick={() =>
                run(async () => {
                  const prompt = await api.getSystemPrompt(controlPort);
                  setOverrideText(prompt.effective_text);
                })
              }
            >
              加载生效 prompt
            </button>
            <button
              disabled={controlsDisabled}
              onClick={() =>
                run(() => api.setSystemPromptOverride(controlPort, overrideText || null), true)
              }
            >
              保存覆盖
            </button>
            <button
              disabled={controlsDisabled}
              onClick={() => {
                setOverrideText('');
                return run(() => api.setSystemPromptOverride(controlPort, null), true);
              }}
            >
              清空覆盖
            </button>
          </div>

          <h3>虚拟连接</h3>
          <textarea
            value={virtualConnectionsText}
            onChange={e => setVirtualConnectionsText(e.target.value)}
            rows={3}
            placeholder="每行一个 participant_id"
          />
          <div className="row-buttons">
            <button
              disabled={controlsDisabled}
              onClick={() =>
                run(
                  () =>
                    api.patchConfig(controlPort, {
                      virtual_connections: virtualConnectionsText
                        .split(/\r?\n/)
                        .map(item => item.trim())
                        .filter(Boolean),
                    }),
                  true
                )
              }
            >
              保存虚拟连接
            </button>
          </div>
          {state?.outbound_messages && state.outbound_messages.length > 0 && (
            <>
              <h3>发送记录</h3>
              <div className="transcript">
                {state.outbound_messages.slice(-8).map((m, i) => (
                  <div key={`${m.timestamp}-${i}`} className="transcript-row role-tool">
                    <span className="transcript-role">{m.participant_id}</span>
                    <div className="transcript-body">
                      {m.conversation_id && (
                        <span className="transcript-tool-result">conversation_id: {m.conversation_id}</span>
                      )}
                      <span className="transcript-content">{m.message || renderJson(m.extra)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          <h3>元信息</h3>
          <label>
            label
            <input value={label} onChange={e => setLabel(e.target.value)} />
          </label>
          <label>
            note
            <input value={note} onChange={e => setNote(e.target.value)} />
          </label>
          <div className="row-buttons">
            <button
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api.patchBranch(branch.id, { label, note });
                  onBranchPatched();
                })
              }
            >
              保存
            </button>
            <VerdictButtons branchId={branch.id} disabled={busy} onSaved={onBranchPatched} run={run} />
          </div>

          <h3>Fork</h3>
          <input
            placeholder="这条想验证什么"
            value={forkLabel}
            onChange={e => setForkLabel(e.target.value)}
          />
          <div className="row-buttons">
            <button
              disabled={controlsDisabled}
              onClick={() =>
                run(async () => {
                  const result = await api.forkBranch(branch.id, { label: forkLabel });
                  setForkLabel('');
                  onForked(result.branch_id);
                })
              }
            >
              从当前状态 fork
            </button>
          </div>

          {state && state.active_bubbles && state.active_bubbles.length > 0 && (
            <>
              <h3>活跃泡泡</h3>
              <ul className="bubble-list">
                {state.active_bubbles.map(b => (
                  <li key={b.id}>
                    [{b.kind}] {b.goal}（{b.status}, {b.cycles_used}/{b.max_cycles}）
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

function PushInputForm({
  controlPort,
  disabled,
  onError,
}: {
  controlPort: number;
  disabled: boolean;
  onError: (msg: string | null) => void;
}) {
  const [content, setContent] = useState('');
  const [participantId, setParticipantId] = useState('explore_lab');
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedParticipantId = participantId.trim() || 'explore_lab';
    if (!content.trim()) return;
    try {
      await api.pushInput(controlPort, content, trimmedParticipantId);
      setParticipantId(trimmedParticipantId);
      setContent('');
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : '发送失败');
    }
  };
  return (
    <form onSubmit={submit} className="input-form">
      <input
        className="participant-input"
        value={participantId}
        onChange={e => setParticipantId(e.target.value)}
        placeholder="participant_id"
        disabled={disabled}
      />
      <input
        value={content}
        onChange={e => setContent(e.target.value)}
        placeholder="给这个分支发一条消息（进 inbox，等 step/resume 处理）"
        disabled={disabled}
      />
      <button type="submit" disabled={disabled}>
        发送
      </button>
    </form>
  );
}

function VerdictButtons({
  branchId,
  disabled,
  onSaved,
  run,
}: {
  branchId: string;
  disabled: boolean;
  onSaved: () => void;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const setVerdict = (result: 'pass' | 'fail' | 'unclear') =>
    run(async () => {
      await api.patchBranch(branchId, { verdict: { result } });
      onSaved();
    });
  return (
    <>
      <button disabled={disabled} onClick={() => setVerdict('pass')}>
        ✓ pass
      </button>
      <button disabled={disabled} onClick={() => setVerdict('fail')}>
        ✗ fail
      </button>
      <button disabled={disabled} onClick={() => setVerdict('unclear')}>
        ? unclear
      </button>
    </>
  );
}
