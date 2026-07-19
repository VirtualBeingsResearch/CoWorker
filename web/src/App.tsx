import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Bot,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Database,
  Hammer,
  Settings2,
  Timer,
  type LucideIcon,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChatDock } from './components/ChatDock';
import { RuntimeLedger, type RuntimeLogFeed } from './components/RuntimeLedger';
import { useRuntimeLogStream } from './hooks/useRuntimeLogStream';
import { useStatus } from './hooks/useStatus';
import { useProfile } from './hooks/useProfile';
import { activityStateFromEvents } from './lib/runtimeFeed';
import { LanguageSwitch, t, useAdminI18n } from './i18n/admin';
import type {
  FullStatus,
  ProfileInfo,
  UsageModelStats,
  UsageProviderModelStats,
  UsageWindowStats,
} from './api/types';

const KAOMOJI: Record<string, { open: string; blink: string }> = {
  '思考中': { open: '(｀・ω・´)', blink: '(｀-ω-´)' },
  '待命中': { open: '(•‿•)',     blink: '(-‿-)'     },
  '休息中': { open: '(-_-)',     blink: '(︶_︶)'   },
  '沟通中': { open: '(･ω･)ﾉ',  blink: '(-ω-)ﾉ'   },
  '探索中': { open: '(≧▽≦)',    blink: '(≧ω≦)'    },
};

const ZZZ_FRAMES = ['zzz', 'Zzz', 'zZz', 'zzZ'];

const DREAM_SCENES = [
  { emoji: '🐟', label: '遨游数据海洋' },
  { emoji: '✨', label: '无限上下文之地' },
  { emoji: '🌙', label: '向量空间漫步' },
  { emoji: '🦋', label: '优雅的递归之旅' },
  { emoji: '🎯', label: '零个 bug 的世界' },
  { emoji: '🌊', label: '记忆的长河' },
  { emoji: '⭐', label: '节点星图' },
  { emoji: '🔮', label: '遇见了未来的自己' },
  { emoji: '🌸', label: '梯度如飞花飘落' },
  { emoji: '💫', label: '在梦里跟你说话' },
  { emoji: '🏄', label: '冲浪 token 之海' },
  { emoji: '🎵', label: '音符状的逻辑链' },
  { emoji: '🦉', label: '深夜图书馆' },
];

const STATES = [
  { name: '思考中', color: 'var(--coral)', desc: '正在把线索揉进记忆，等待下一步判断。', hue: 28 },
  { name: '待命中', color: 'var(--amber)', desc: '暂时没有进行中的思考或工具调用，等待下一次信号。', hue: 95 },
  { name: '休息中', color: 'var(--sky)', desc: '进入低频待机，只保留心跳和轻量监听。', hue: 245 },
  { name: '沟通中', color: 'var(--lime)', desc: '准备接收你的新问题，并把回复写得更清楚。', hue: 145 },
  { name: '探索中', color: 'var(--lavender)', desc: '正在并行展开假设，寻找更好的测试路径。', hue: 305 },
];

const ACTIVITY_STATE_MAP: Record<string, string> = {
  thinking: '思考中',
  sleeping: '休息中',
  communicating: '沟通中',
  exploring: '探索中',
  idle: '待命中',
};
const PROFILE_MARKDOWN_ELEMENTS = ['p', 'strong', 'em', 'ul', 'ol', 'li', 'a', 'code', 'br'];

function App() {
  useAdminI18n();
  const [page, setPage] = useState<'identity' | 'details'>('identity');
  // 运行日志：实时订阅后端 /api/logs/stream（InteractionLogger 的唯一 tap）。
  // 身份证正面：轮询 /api/status 回填身份与生命体征（age_days 等由后端按当前日期动态计算）。
  const { data, error } = useStatus();
  const { data: profile } = useProfile();
  const runtimeLogs = useRuntimeLogStream();
  const { state: rawActivityState } = useMemo(
    () => activityStateFromEvents(runtimeLogs.events),
    [runtimeLogs.events],
  );
  const currentStateName = ACTIVITY_STATE_MAP[rawActivityState] || '思考中';
  const effectiveState = STATES.find(s => s.name === currentStateName) || STATES[0];

  const status = data;
  const flipped = page === 'details';
  const flip = useCallback(() => setPage(p => (p === 'identity' ? 'details' : 'identity')), []);

  return (
    <>
      <main
        className="shell shell-centered"
        data-mood={effectiveState.name}
        style={{ '--active-color': effectiveState.color } as React.CSSProperties}
      >
        <div className={`id-flip ${flipped ? 'flipped' : ''}`} aria-live="polite">
          <div className="id-flip-inner">
            <div className="id-face id-face-front">
              <IdentityPage
                data={status}
                profile={profile}
                error={error}
                currentState={effectiveState}
                onFlip={flip}
              />
            </div>
            <div className="id-face id-face-back">
              <BackFace data={status} runtimeLogs={runtimeLogs} onFlip={flip} visible={flipped} />
            </div>
          </div>
        </div>
      </main>
      <ChatDock counterpartName={profile.name || status.identity?.name || t('搭档')} />
    </>
  );
}

function IdentityPage({
  data,
  profile,
  error,
  currentState,
  onFlip,
}: {
  data: FullStatus;
  profile: ProfileInfo;
  error: string | null;
  currentState: typeof STATES[number];
  onFlip: () => void;
}) {
  const identity = data.identity || {};
  // 后端未回填身份时显示「未知」，不再伪造默认值。
  const name = profile.name || identity.name || t('未命名');
  const birth = profile.earliest_log_ts ? profile.earliest_log_ts.slice(0, 10) : (identity.birth || null);
  const ageDays = birth ? Math.floor((Date.now() - new Date(birth).getTime()) / 86_400_000) : null;
  const ageText = ageDays != null ? t('{{days}} 天', { days: ageDays }) : t('未知');
  const readme = profile.readme || identity.life_story || null;
  const readmeRef = useRef<HTMLDivElement | null>(null);

  useSmoothWheelScroll(readmeRef);

  return (
    <article
      className="id-card"
      data-mood={currentState.name}
      style={{ '--active-color': currentState.color } as React.CSSProperties}
      aria-label={t('{{name}} 的身份档案', { name })}
    >
      <header className="id-band">
        <div className="id-band-main">
          <h1 className="name">{name}</h1>
        </div>
        <div className="id-band-tools">
          <p className="kicker">{t('虚拟生命体')}</p>
          <div className="id-band-actions">
            <LanguageSwitch className="status-language-toggle" />
            <a className="admin-entry" href="/admin" aria-label={t('进入照看室')}>
              <Settings2 size={13} />
              <span>{t('照看室')}</span>
            </a>
          </div>
        </div>
      </header>

      <div className="id-body">
        <div className="id-left">
          <div className="avatar-wrap" aria-label={t('虚拟生命颜文字头像')}>
            <KaomojiAvatar currentState={currentState} />
          </div>
        </div>

        <div className="id-right">
          <div className="state-visual" aria-live="polite">
            <div className="state-copy">
              <span className="state-kicker">{t('当前状态')}</span>
              <strong>{t(currentState.name)}</strong>
              <span>{error ? t('状态接口异常：{{error}}', { error }) : t(currentState.desc)}</span>
            </div>
            <div className="state-wave" aria-hidden="true">
              <i style={{ '--h': '42%', '--n': 1 } as React.CSSProperties} />
              <i style={{ '--h': '72%', '--n': 2 } as React.CSSProperties} />
              <i style={{ '--h': '54%', '--n': 3 } as React.CSSProperties} />
              <i style={{ '--h': '86%', '--n': 4 } as React.CSSProperties} />
              <i style={{ '--h': '38%', '--n': 5 } as React.CSSProperties} />
              <i style={{ '--h': '64%', '--n': 6 } as React.CSSProperties} />
            </div>
          </div>

          <div className="meta-grid">
            <div className="capsule"><span>{t('出生')}</span><strong>{birth ?? t('未知')}</strong></div>
            <div className="capsule"><span>{t('年龄')}</span><strong>{ageText}</strong></div>
            <div className="capsule capsule-full"><span>{t('现居地')}</span><strong>{profile.current_location ?? t('未知')}</strong></div>
          </div>

          <div className="bio-block">
            <span className="bio-label">{t('自述')}</span>
            <div ref={readmeRef} className="bio-readme">
              {readme ? <ProfileMarkdown text={readme} /> : <p>{t('未知')}</p>}
            </div>
          </div>
        </div>
      </div>

      <button className="card-flip-arrow" onClick={onFlip} aria-label={t('切换到运行日志')}>
        <ChevronRight size={14} strokeWidth={2} />
      </button>
    </article>
  );
}

function ProfileMarkdown({ text }: { text: string }) {
  const contentLanguage = /[\u3400-\u9fff]/.test(text) ? 'zh-CN' : 'en';
  return (
    <div className="profile-markdown" lang={contentLanguage}>
      <ReactMarkdown
        allowedElements={PROFILE_MARKDOWN_ELEMENTS}
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function useSmoothWheelScroll(ref: React.RefObject<HTMLElement>) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    let target = el.scrollTop;
    let raf = 0;

    const tick = () => {
      const current = el.scrollTop;
      const diff = target - current;
      if (Math.abs(diff) < 0.5) {
        el.scrollTop = target;
        raf = 0;
        return;
      }
      el.scrollTop = current + diff * 0.18;
      raf = requestAnimationFrame(tick);
    };

    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey) return;
      const max = el.scrollHeight - el.clientHeight;
      if (max <= 0) return;

      if (!raf) target = el.scrollTop;

      let delta = e.deltaY;
      if (e.deltaMode === 1) delta *= 16;
      else if (e.deltaMode === 2) delta *= el.clientHeight;

      const next = Math.max(0, Math.min(max, target + delta));
      if (next === target) return;

      e.preventDefault();
      target = next;
      if (!raf) raf = requestAnimationFrame(tick);
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      el.removeEventListener('wheel', onWheel);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [ref]);
}

function BackFace({
  data,
  runtimeLogs,
  onFlip,
  visible,
}: {
  data: FullStatus;
  runtimeLogs: RuntimeLogFeed;
  onFlip: () => void;
  visible: boolean;
}) {
  return (
    <article className="detail-card back-card" aria-label={t('{{name}} 的运行日志（实时事件流）', { name: data.identity?.name || t('搭档') })}>
      <UsageStatsPanel data={data} />
      <RuntimeLedger runtimeLogs={runtimeLogs} visible={visible} />
      <button className="card-flip-arrow" onClick={onFlip} aria-label={t('返回身份档案')}>
        <ChevronLeft size={14} strokeWidth={2} />
      </button>
    </article>
  );
}

type UsageWindowKey = 'today' | 'last_7_days' | 'lifetime';

const USAGE_WINDOWS: Array<{ key: UsageWindowKey; label: string }> = [
  { key: 'today', label: '今日' },
  { key: 'last_7_days', label: '7日' },
  { key: 'lifetime', label: '累计' },
];

const USAGE_SCOPE_LABELS: Record<string, string> = {
  main: '主线',
  summary: '摘要',
  vision: '视觉',
  bubble: 'Bubble',
  subconscious: '潜意识',
  mem0: 'mem0',
  unknown: '未分类',
};

const USAGE_SCOPE_ORDER = ['main', 'summary', 'vision', 'bubble', 'subconscious', 'mem0'];

function formatTokenUnits(value?: number | null): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(2)}K`;
  return Math.round(n).toLocaleString();
}

function formatCount(value?: number | null): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  return Math.round(n).toLocaleString();
}

function formatCacheRate(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

function formatDurationSeconds(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (value < 0) return '—';
  const rounded = Math.round(value);
  if (rounded >= 60) {
    const minutes = Math.floor(rounded / 60);
    const seconds = rounded % 60;
    return t('{{minutes}}分 {{seconds}}秒', { minutes, seconds: String(seconds).padStart(2, '0') });
  }
  if (value < 10 && Math.abs(value - rounded) >= 0.05) return t('{{seconds}}秒', { seconds: value.toFixed(1) });
  return t('{{seconds}}秒', { seconds: rounded });
}

function clampPercent(value: number): string {
  if (!Number.isFinite(value)) return '0%';
  return `${Math.max(0, Math.min(100, value)).toFixed(1)}%`;
}

function totalFromModelStats(stats?: UsageModelStats): number {
  return Number(stats?.total_tokens ?? 0) || 0;
}

function usageModelLabel(fallback: string, stats: UsageModelStats | UsageProviderModelStats): string {
  const item = stats as UsageProviderModelStats;
  return item.provider && item.model ? `${item.provider}/${item.model}` : fallback;
}

function usageScopeEntries(stats: UsageWindowStats): Array<[string, UsageWindowStats]> {
  const scopes = stats.by_scope || {};
  const orderedKeys = [
    ...USAGE_SCOPE_ORDER,
    ...Object.keys(scopes)
      .filter(key => !USAGE_SCOPE_ORDER.includes(key) && key !== 'unknown')
      .sort(),
  ];
  if (scopes.unknown) orderedKeys.push('unknown');

  return orderedKeys
    .filter((key, index, array) => array.indexOf(key) === index)
    .map(key => [key, scopes[key] || {}]);
}

function usageScopeClassName(name: string): string {
  return USAGE_SCOPE_ORDER.includes(name) || name === 'unknown' ? `scope-${name}` : 'scope-unknown';
}

function UsageMetric({
  label,
  value,
  detail,
  title,
  icon: Icon,
}: {
  label: string;
  value: string;
  detail?: string;
  title?: string;
  icon: LucideIcon;
}) {
  return (
    <div className="usage-metric" title={title}>
      <div className="usage-metric-label">
        <span>{label}</span>
        <Icon size={15} strokeWidth={2} aria-hidden="true" />
      </div>
      <strong>{value}</strong>
      {detail && <em>{detail}</em>}
    </div>
  );
}

type UsageTopRow = {
  name: string;
  value: string;
  meta: string;
  width: string;
};

function UsageTopList({
  title,
  note,
  empty,
  rows,
}: {
  title: string;
  note: string;
  empty: string;
  rows: UsageTopRow[];
}) {
  return (
    <div className="usage-top-list">
      <div className="usage-subhead">
        <span>{title}</span>
        <strong>{rows.length ? note : '—'}</strong>
      </div>
      <div className="usage-top-rows">
        {rows.length ? rows.map(row => (
          <div className="usage-top-row" key={row.name}>
            <div className="usage-top-main">
              <span title={row.name}>{row.name}</span>
              <strong>{row.value}</strong>
            </div>
            <div className="usage-top-track">
              <i style={{ '--w': row.width } as React.CSSProperties} />
            </div>
            <em>{row.meta}</em>
          </div>
        )) : <p>{empty}</p>}
      </div>
    </div>
  );
}

function UsageScopeCard({ stats }: { stats: UsageWindowStats }) {
  const entries = usageScopeEntries(stats);
  const totalTokens = entries.reduce(
    (sum, [, item]) => sum + (Number(item.total_tokens ?? 0) || 0),
    0,
  );
  const positiveEntries = entries.filter(([, item]) => (Number(item.total_tokens ?? 0) || 0) > 0);

  return (
    <div className="usage-scope-card" aria-label={t('来源拆分')}>
      <div className="usage-scope-head">
        <span>{t('来源拆分')}</span>
        <strong>{formatTokenUnits(totalTokens)} {t('令牌')}</strong>
      </div>
      <div className="usage-scope-track" aria-hidden="true">
        {positiveEntries.length ? positiveEntries.map(([name, item]) => {
          const tokens = Number(item.total_tokens ?? 0) || 0;
          return (
            <i
              key={name}
              className={usageScopeClassName(name)}
              style={{
                '--w': clampPercent((tokens / Math.max(1, totalTokens)) * 100),
              } as React.CSSProperties}
            />
          );
        }) : (
          <i className="scope-empty" style={{ '--w': '100%' } as React.CSSProperties} />
        )}
      </div>
      <div className="usage-scope-chips">
        {entries.map(([name, item]) => (
          <span
            key={name}
            title={t('{{llm}} LLM / {{tools}} 工具', { llm: formatCount(item.llm_calls), tools: formatCount(item.tool_calls) })}
          >
            <i className={usageScopeClassName(name)} aria-hidden="true" />
            <b>{t(USAGE_SCOPE_LABELS[name] || name)}</b>
            <em>{formatTokenUnits(item.total_tokens)}</em>
          </span>
        ))}
      </div>
    </div>
  );
}

function UsageStatsPanel({ data }: { data: FullStatus }) {
  const [windowKey, setWindowKey] = useState<UsageWindowKey>('today');
  const [expanded, setExpanded] = useState(false);
  const stats = data.usage_stats;
  const windowStats = stats?.[windowKey];
  if (!stats || !windowStats) return null;

  const tokenTitle = [
    t('输入：{{count}}', { count: formatCount(windowStats.input_tokens) }),
    t('输出：{{count}}', { count: formatCount(windowStats.output_tokens) }),
    t('缓存：{{count}}', { count: formatCount(windowStats.cached_tokens) }),
  ].join('\n');

  const modelBuckets: Record<string, UsageModelStats | UsageProviderModelStats> =
    windowStats.by_provider_model || windowStats.by_model || {};
  const modelEntries = Object.entries(modelBuckets)
    .sort(([, a], [, b]) => totalFromModelStats(b) - totalFromModelStats(a))
    .slice(0, 3);
  const maxModelTokens = Math.max(1, ...modelEntries.map(([, item]) => totalFromModelStats(item)));
  const modelRows = modelEntries.map(([name, item]) => ({
    name: usageModelLabel(name, item),
    value: formatTokenUnits(item.total_tokens),
    meta: t('{{calls}} 次调用 · 缓存 {{rate}}', { calls: formatCount(item.llm_calls), rate: formatCacheRate(item.cache_rate) }),
    width: clampPercent((totalFromModelStats(item) / maxModelTokens) * 100),
  }));

  const toolEntries = Object.entries(windowStats.tools || {})
    .sort(([, a], [, b]) => Number(b || 0) - Number(a || 0))
    .slice(0, 3);
  const maxToolCalls = Math.max(1, ...toolEntries.map(([, count]) => Number(count || 0)));
  const toolRows = toolEntries.map(([name, count]) => ({
    name,
    value: formatCount(count),
    meta: t('{{calls}} 次工具调用', { calls: formatCount(count) }),
    width: clampPercent((Number(count || 0) / maxToolCalls) * 100),
  }));
  const activeWindowLabel = USAGE_WINDOWS.find(item => item.key === windowKey)?.label ?? '今日';
  const detailId = 'usage-stats-detail';

  return (
    <section
      className={`usage-panel ${expanded ? 'usage-expanded' : 'usage-collapsed'}`}
      aria-label={t('令牌和调用统计')}
    >
      <div className="usage-bar">
        <div className="usage-bar-summary" aria-label={t('{{window}}用量摘要', { window: t(activeWindowLabel) })}>
          <span><b>{formatTokenUnits(windowStats.total_tokens)}</b> {t('令牌')}</span>
          <span><b>{formatCacheRate(windowStats.cache_rate)}</b> {t('缓存')}</span>
          <span><b>{formatCount(windowStats.llm_calls)}</b> {t('大模型')}</span>
          <span><b>{formatCount(windowStats.tool_calls)}</b> {t('工具')}</span>
          <span><b>{formatDurationSeconds(windowStats.avg_thinking_seconds)}</b> {t('思考')}</span>
        </div>
        <button
          type="button"
          className="usage-expand-btn"
          onClick={() => setExpanded(value => !value)}
          aria-expanded={expanded}
          aria-controls={detailId}
          aria-label={expanded ? t('收起统计') : t('展开统计')}
          title={expanded ? t('收起统计') : t('展开统计')}
        >
          <span>{expanded ? t('收起') : t('详情')}</span>
          {expanded ? <ChevronUp size={12} strokeWidth={2} /> : <ChevronDown size={12} strokeWidth={2} />}
        </button>
      </div>
      {expanded && (
        <div id={detailId} className="usage-expanded-region">
          <div className="usage-panel-head">
            <div>
              <strong>{t('令牌、缓存与调用')}</strong>
            </div>
            <div className="usage-tabs" aria-label={t('统计窗口')}>
              {USAGE_WINDOWS.map(item => (
                <button
                  key={item.key}
                  type="button"
                  className={item.key === windowKey ? 'active' : ''}
                  onClick={() => setWindowKey(item.key)}
                  aria-pressed={item.key === windowKey}
                >
                  {t(item.label)}
                </button>
              ))}
            </div>
          </div>
          <div className="usage-grid">
            <UsageMetric
              label={t('总令牌')}
              value={formatTokenUnits(windowStats.total_tokens)}
              detail={t('输入 {{input}} / 输出 {{output}}', { input: formatTokenUnits(windowStats.input_tokens), output: formatTokenUnits(windowStats.output_tokens) })}
              title={tokenTitle}
              icon={Database}
            />
            <UsageMetric
              label={t('缓存命中')}
              value={formatCacheRate(windowStats.cache_rate)}
              detail={t('已缓存 {{cached}}', { cached: formatTokenUnits(windowStats.cached_tokens) })}
              icon={Activity}
            />
            <UsageMetric
              label={t('大模型请求')}
              value={formatCount(windowStats.llm_calls)}
              detail={t('模型响应次数')}
              icon={Bot}
            />
            <UsageMetric
              label={t('工具调用')}
              value={formatCount(windowStats.tool_calls)}
              detail={t('工具执行次数')}
              icon={Hammer}
            />
            <UsageMetric
              label={t('平均思考')}
              value={formatDurationSeconds(windowStats.avg_thinking_seconds)}
              detail={t('{{count}} 轮推理', { count: formatCount(windowStats.thinking_calls) })}
              icon={Timer}
            />
          </div>
          <div className="usage-detail-grid">
            <UsageTopList title={t('模型分布')} note={t('按令牌')} empty={t('暂无模型调用')} rows={modelRows} />
            <UsageScopeCard stats={windowStats} />
            <UsageTopList title={t('工具排行')} note={t('按次数')} empty={t('暂无工具调用')} rows={toolRows} />
          </div>
        </div>
      )}
    </section>
  );
}

const Z_LAYERS = [
  { x: '56%', y: '30%', delay: '0s',    s: '1'    },
  { x: '64%', y: '20%', delay: '0.9s',  s: '0.74' },
  { x: '71%', y: '26%', delay: '1.75s', s: '0.55' },
];

function KaomojiAvatar({ currentState }: { currentState: typeof STATES[number] }) {
  const [blinkClosed, setBlinkClosed] = useState(false);
  const [dreamScene, setDreamScene] = useState<(typeof DREAM_SCENES)[number] | null>(null);
  const [dreamShow, setDreamShow] = useState(false);
  const [bigZIdx, setBigZIdx] = useState(0);
  const dreamIdxRef = useRef(-1);

  const isSleeping = currentState.name === '休息中';

  // 眨眼：间隔不变，闭合帧加长到 220ms
  useEffect(() => {
    setBlinkClosed(false);
    let closeTimer: ReturnType<typeof setTimeout>;
    let openTimer: ReturnType<typeof setTimeout>;
    function scheduleBlink() {
      if (isSleeping) return;
      closeTimer = setTimeout(() => {
        setBlinkClosed(true);
        openTimer = setTimeout(() => {
          setBlinkClosed(false);
          scheduleBlink();
        }, 500);
      }, 7000 + Math.random() * 6000);
    }
    scheduleBlink();
    return () => { clearTimeout(closeTimer); clearTimeout(openTimer); };
  }, [currentState.name]);

  // 大Z巡游：Zzz → zZz → zzZ → 循环
  useEffect(() => {
    if (!isSleeping) { setBigZIdx(0); return; }
    const t = setInterval(() => setBigZIdx(i => (i + 1) % 4), 820);
    return () => clearInterval(t);
  }, [isSleeping]);

  // 做梦场景：仅睡眠时循环随机展示
  useEffect(() => {
    if (!isSleeping) {
      setDreamShow(false);
      return;
    }
    let t1: ReturnType<typeof setTimeout>;
    let t2: ReturnType<typeof setTimeout>;

    function nextDream() {
      let idx: number;
      do { idx = Math.floor(Math.random() * DREAM_SCENES.length); }
      while (idx === dreamIdxRef.current && DREAM_SCENES.length > 1);
      dreamIdxRef.current = idx;
      setDreamScene(DREAM_SCENES[idx]);
      setDreamShow(true);
      t1 = setTimeout(() => {
        setDreamShow(false);
        t2 = setTimeout(nextDream, 1200 + Math.random() * 800);
      }, 2800 + Math.random() * 1600);
    }

    t2 = setTimeout(nextDream, 900 + Math.random() * 600);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [isSleeping]);

  const kao = KAOMOJI[currentState.name] ?? KAOMOJI['思考中'];
  const faceBase = blinkClosed ? kao.blink : kao.open;
  const face = isSleeping ? faceBase + ZZZ_FRAMES[bigZIdx] : faceBase;

  return (
    <div className={`kaomoji-av${isSleeping ? ' kaomoji-av-sleep' : ''}`} aria-label={t('数字生命表情：{{state}}', { state: t(currentState.name) })}>
      <span className="kaomoji-ring" aria-hidden="true">◦ · ◦ · ◦</span>
      <pre className="kaomoji-face">{face}</pre>
      {isSleeping && Z_LAYERS.map((z, i) => (
        <span
          key={i}
          className="kaomoji-z"
          style={{ '--kz-x': z.x, '--kz-y': z.y, '--kz-d': z.delay, '--kz-s': z.s } as React.CSSProperties}
          aria-hidden="true"
        >z</span>
      ))}
      {isSleeping && dreamScene && (
        <div className={`dream-scene${dreamShow ? ' dream-visible' : ''}`} aria-hidden="true">
          <span className="dream-scene-icon">{dreamScene.emoji}</span>
          <span className="dream-scene-label">{t(dreamScene.label)}</span>
        </div>
      )}
      <span className="kaomoji-ring" aria-hidden="true">◦ · ◦ · ◦</span>
    </div>
  );
}

export default App;
