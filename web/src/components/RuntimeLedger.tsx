import { useEffect, useMemo, useRef, useState } from 'react';
import type { RuntimeLogEvent } from '../api/types';
import { deriveFeedRows, type FeedRow } from '../lib/runtimeFeed';
import { t, useAdminI18n } from '../i18n/admin';

// 运行日志的数据源形状（useRuntimeLogStream 的返回值）：events 来自后端
// /api/logs/stream（InteractionLogger → RuntimeEventCollector 的实时 SSE 流）。
export type RuntimeLogFeed = { events: RuntimeLogEvent[]; error?: string | null };

// 身份证背面 · 运行日志（只展示实时事件流）：每类型专属图标与动效，
// 工具调用↔结果同行合并，thinking 长态生命周期（thinking_start→llm_response 收口），
// 上滑看历史不被拉回 + 回到最新药丸，新消息巨型 emoji 冲屏。无标题/页脚，纯日志。

const KIND_CLASS: Record<string, string> = {
  msg_in: 'msg_in',
  msg_out: 'msg_out',
  skill_load: 'skill_load',
  raw: 'raw',
};

function rowClass(row: FeedRow): string {
  if (row.kind === 'thinking') return row.status === 'done' ? 'le thinking done' : 'le thinking';
  if (row.kind === 'sleep') return row.status === 'done' ? 'le sleep done' : 'le sleep active';
  if (row.kind === 'tool') {
    if (row.status === 'ok') return 'le tool_ok resolved';
    if (row.status === 'err') return 'le tool_err resolved';
    return 'le tool_call active';
  }
  return `le ${KIND_CLASS[row.kind] || 'raw'}`;
}

// 后端 ts 是 ISO（2026-06-11T02:15:00.123456）；日志只展示到秒的 HH:MM:SS。
function fmtTime(ts?: string): string {
  if (!ts) return '--:--:--';
  const t = ts.indexOf('T');
  return t >= 0 ? ts.slice(t + 1, t + 9) : ts.slice(0, 8);
}

function fmtDuration(durationMs?: number): string | null {
  if (typeof durationMs !== 'number' || !Number.isFinite(durationMs) || durationMs < 0) return null;
  if (durationMs < 1_000) return t('{{milliseconds}}毫秒', { milliseconds: Math.round(durationMs) });

  const seconds = durationMs / 1_000;
  if (seconds < 10) return t('{{seconds}}秒', { seconds: seconds.toFixed(1) });

  const rounded = Math.round(seconds);
  if (rounded >= 60) {
    return t('{{minutes}}分 {{seconds}}秒', {
      minutes: Math.floor(rounded / 60),
      seconds: String(rounded % 60).padStart(2, '0'),
    });
  }
  return t('{{seconds}}秒', { seconds: rounded });
}

// 骨架屏：数据到达前的占位行（布局与 .le 完全对齐）
const SK_ROWS = [
  { tw: '72%', bw: '58%', ac: 'color-mix(in oklch, var(--ledger-in) 36%, transparent)' },
  { tw: '68%', bw: '82%', ac: 'color-mix(in oklch, var(--ledger-out) 34%, transparent)' },
  { tw: '76%', bw: '46%', ac: 'color-mix(in oklch, var(--ledger-thinking) 32%, transparent)' },
  { tw: '65%', bw: '74%', ac: 'color-mix(in oklch, var(--ledger-sleep) 34%, transparent)' },
  { tw: '70%', bw: '35%', ac: 'color-mix(in oklch, var(--ledger-skill) 34%, transparent)' },
];

function LedgerSkeleton() {
  return (
    <div className="ledger-skeleton" aria-hidden="true">
      {SK_ROWS.map((r, i) => (
        <div key={i} className="sk-row" style={{ '--sk-delay': `${i * 0.08}s`, '--sk-accent': r.ac } as React.CSSProperties}>
          <div className="sk-bone" style={{ width: r.tw }} />
          <div className="sk-bone sk-icon" />
          <div className="sk-body">
            <div className="sk-bone sk-tag" />
            <div className="sk-bone" style={{ width: r.bw }} />
          </div>
        </div>
      ))}
    </div>
  );
}

// 活跃状态（正在进行中）的行不跳过动效，只跳过已结算的历史行
function isSettled(row: FeedRow): boolean {
  if (row.kind === 'thinking') return row.status === 'done';
  if (row.kind === 'sleep') return row.status === 'done';
  if (row.kind === 'tool') return row.status === 'ok' || row.status === 'err';
  return true;
}

function LedgerRow({ row, initial }: { row: FeedRow; initial?: boolean }) {
  const noAnim = initial && isSettled(row);
  const duration = fmtDuration(row.durationMs);
  return (
    <div className={`${rowClass(row)}${row.cls ? ` ${row.cls}` : ''}${noAnim ? ' no-anim' : ''}`}>
      <span className="le-time">{fmtTime(row.ts)}</span>
      <span className="le-icon">{row.icon}</span>
      <span className="le-body">
        <span className="le-heading">
          <span className="le-tag">{row.tag}</span>
          {duration && <span className="le-duration">{duration}</span>}
        </span>
        {(row.text || row.dots) && (
          <span className="le-text">
            {row.text}
            {row.dots && <span className="dots" />}
          </span>
        )}
      </span>
    </div>
  );
}

interface Hero {
  id: number;
  type: 'msg_in' | 'msg_out';
}

export function RuntimeLedger({
  runtimeLogs,
  visible,
}: {
  runtimeLogs: RuntimeLogFeed;
  visible?: boolean;
}) {
  const { language } = useAdminI18n();
  const rows = useMemo(() => deriveFeedRows(runtimeLogs.events), [language, runtimeLogs.events]);

  // 首次翻开时逐行插入动画：先展示最新行，再向上依次插入历史行
  const [revealCount, setRevealCount] = useState(0);
  const [revealDone, setRevealDone] = useState(false);
  const hasRevealedRef = useRef(false);
  const prevVisibleRef = useRef(false);
  const revealIvRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    const became = visible && !prevVisibleRef.current;
    prevVisibleRef.current = !!visible;
    if (!became || hasRevealedRef.current) return;
    hasRevealedRef.current = true;

    if (rows.length === 0) { setRevealDone(true); return; }

    const total = rows.length;
    let n = 1; // 最新行（rows[total-1]）立即可见，通过 Math.max(1, revealCount) 保证
    revealIvRef.current = setInterval(() => {
      n++;
      setRevealCount(n);
      if (n >= total) {
        clearInterval(revealIvRef.current!);
        setRevealDone(true);
      }
    }, 30);
    return () => { if (revealIvRef.current) clearInterval(revealIvRef.current); };
  }, [visible]);


  const feedRef = useRef<HTMLDivElement | null>(null);
  const [following, setFollowing] = useState(true);
  const followingRef = useRef(true);
  // 自身正在程序化平滑贴底：用来区分「平滑动画途中的 scroll 事件」与「用户主动上滑」，
  // 否则平滑滚动尚未到底时 onScroll 会误判成离底而中断跟随。
  const autoRef = useRef(false);
  const [unseen, setUnseen] = useState(0);
  const prevLenRef = useRef(0);

  // hero 大动画 + 已触发过的消息行 key
  const [heroes, setHeroes] = useState<Hero[]>([]);
  const seenMsgRef = useRef<Set<string>>(new Set());
  const heroIdRef = useRef(0);

  followingRef.current = following;

  // 首次加载时已存在的历史行：跳过入场动效 + 不触发 Hero Burst
  const initialKeysRef = useRef<Set<string>>(new Set());

  // 近底部的增量平滑落底（丝滑），大跨度跳跃用瞬时（避免长动画拖沓）。
  const scrollToBottom = (forceSmooth?: boolean) => {
    const el = feedRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distance <= 1) return;
    const smooth = forceSmooth ?? distance < 800;
    autoRef.current = true;
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    } else {
      el.scrollTop = el.scrollHeight;
      requestAnimationFrame(() => {
        if (followingRef.current && feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
      });
    }
  };

  const scrollToLatest = () => {
    setFollowing(true);
    setUnseen(0);
    followingRef.current = true;
    scrollToBottom(true);
  };

  // 用户主动滚轮/触摸 → 立刻交出控制权，使随后的 onScroll 按「用户上滑」判定。
  const releaseAuto = () => {
    autoRef.current = false;
  };

  const onScroll = () => {
    const el = feedRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atBottom) {
      autoRef.current = false;
      setFollowing(true);
      setUnseen(0);
    } else if (!autoRef.current) {
      setFollowing(false);
    }
  };

  // 行变化时：在底部就（平滑）贴底，否则累计未读
  useEffect(() => {
    const added = Math.max(0, rows.length - prevLenRef.current);
    const first = prevLenRef.current === 0;
    // 第一批数据：记录初始行 key（跳过入场动效）+ 预填 seenMsgRef（阻止 Hero Burst）
    if (first && rows.length > 0) {
      rows.forEach(r => {
        initialKeysRef.current.add(r.key);
        if (r.kind === 'msg_in' || r.kind === 'msg_out') seenMsgRef.current.add(r.key);
      });
    }
    prevLenRef.current = rows.length;
    if (followingRef.current) {
      scrollToBottom(first ? false : undefined);
    } else if (added > 0) {
      setUnseen(u => u + added);
    }
  }, [rows]);

  // 逐行插入完成后跳到最新行
  useEffect(() => {
    if (!revealDone) return;
    requestAnimationFrame(() => scrollToBottom(false));
  }, [revealDone]);

  // 平滑滚轮：把离散的滚轮步进累积成目标位置，用 rAF 指数缓动逼近，得到丝滑滚动手感
  // （这块滚动区嵌在 preserve-3d 翻转卡内，原生滚轮多在主线程逐档重绘、又跳又顿）。
  // 触控板/触屏本就有连续动量，到边界时放行默认，不抢它们的手感。
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    let target = el.scrollTop;
    let raf = 0;
    const tick = () => {
      const cur = el.scrollTop;
      const diff = target - cur;
      if (Math.abs(diff) < 0.5) {
        el.scrollTop = target;
        raf = 0;
        return;
      }
      el.scrollTop = cur + diff * 0.18;
      raf = requestAnimationFrame(tick);
    };
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey) return; // 缩放手势放行
      const max = el.scrollHeight - el.clientHeight;
      if (max <= 0) return;
      autoRef.current = false; // 用户接管滚动
      if (!raf) target = el.scrollTop; // 静止时重新对齐起点，避免与自动贴底冲突后跳变
      let delta = e.deltaY;
      if (e.deltaMode === 1) delta *= 16; // 行 → 像素
      else if (e.deltaMode === 2) delta *= el.clientHeight; // 页 → 像素
      const next = Math.max(0, Math.min(max, target + delta));
      if (next === target) return; // 已到边界：放行默认
      e.preventDefault();
      target = next;
      if (!raf) raf = requestAnimationFrame(tick);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      el.removeEventListener('wheel', onWheel);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  // 新消息行触发巨型 emoji 冲屏（每个 key 仅一次）
  useEffect(() => {
    const fresh: Hero[] = [];
    for (const r of rows) {
      if ((r.kind === 'msg_in' || r.kind === 'msg_out') && !seenMsgRef.current.has(r.key)) {
        seenMsgRef.current.add(r.key);
        fresh.push({ id: ++heroIdRef.current, type: r.kind });
      }
    }
    if (!fresh.length) return;
    setHeroes(h => [...h, ...fresh]);
    const ids = new Set(fresh.map(f => f.id));
    const timer = setTimeout(() => setHeroes(h => h.filter(x => !ids.has(x.id))), 1250);
    return () => clearTimeout(timer);
  }, [rows]);

  return (
    <div className="ledger" aria-label={t('{{name}} 的运行日志（实时事件流）', { name: t('搭档') })}>
      <div className="ledger-feed-wrap">
        <div
          className="ledger-feed"
          ref={feedRef}
          onScroll={onScroll}
          onTouchStart={releaseAuto}
          aria-live="polite"
        >
          {runtimeLogs.error ? (
            <div className="ledger-empty">{t('日志流：{{error}}', { error: runtimeLogs.error })}</div>
          ) : rows.length === 0 ? (
            <LedgerSkeleton />
          ) : (
            (revealDone ? rows : rows.slice(0, Math.max(1, revealCount))).map(row =>
              <LedgerRow key={row.key} row={row} initial={initialKeysRef.current.has(row.key)} />
            )
          )}
        </div>
        <button
          className={`jump-btn ${!following && unseen > 0 ? 'show' : ''}`}
          onClick={scrollToLatest}
          aria-label={t('回到最新日志')}
        >
          <span className="dot" />
          <b>{t('{{count}} 条新日志', { count: unseen > 99 ? '99+' : unseen })}</b> <span className="arr">↓</span>
        </button>

        {heroes.map(h => (
          <HeroBurst key={h.id} type={h.type} />
        ))}
      </div>
    </div>
  );
}

function HeroBurst({ type }: { type: 'msg_in' | 'msg_out' }) {
  const color = type === 'msg_in' ? 'var(--ledger-in)' : 'var(--ledger-out)';
  const fc = { '--fc': color } as React.CSSProperties;
  return (
    <>
      <div className="hero-flash" style={fc} />
      <div className="hero-shock" style={fc} />
      <div className={`hero ${type === 'msg_in' ? 'in' : 'out'}`}>{type === 'msg_in' ? '📨' : '✈️'}</div>
    </>
  );
}
