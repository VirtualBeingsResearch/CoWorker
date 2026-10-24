/**
 * English singular overrides for count-based phrases.
 *
 * The admin catalog uses the Chinese source string as its stable key. English
 * values keep the plural form, and this table provides the singular form used
 * when `values.count` is exactly one. Only phrases whose English noun (or verb)
 * actually change need an entry; everything else keeps the catalog value.
 */
export const ENGLISH_SINGULAR_OVERRIDES: Record<string, string> = {
  '显示全部 {{count}} 种币种': 'Show all {{count}} currency',
  '定价覆盖 {{rate}} · {{count}} 未定价 Token': '{{rate}} pricing coverage · {{count}} unpriced token',
  '本地估算 · 定价覆盖率 {{coverage}} · {{count}} Token 未定价': 'Local estimate · {{coverage}} pricing coverage · {{count}} unpriced token',
  '缓存 Token {{count}}': '{{count}} cached token',
  '{{count}} Token 尚未定价，金额仅包含已定价部分。': '{{count}} token is unpriced; the amount includes only priced usage.',
  '{{count}} 次模型调用没有可记录的 Token。': '{{count}} model call has no trackable token data.',
  '{{count}} 次调用使用估算 Token，已与精确值分开。': '{{count}} call uses estimated tokens and is separated from exact values.',
  '{{count}} 次工具调用返回错误。': '{{count}} tool call returned an error.',
  '{{count}} 次显式技能加载失败。': '{{count}} explicit skill load failed.',
  '单次平均 {{count}} Token': '{{count}} token per measurable call',
  '{{count}} 错误': '{{count}} error',
  '{{count}} 显式成功': '{{count}} explicit success',
  '{{count}} 显式失败': '{{count}} explicit failure',
  '{{count}} 自动加载': '{{count}} auto-load',
  '{{count}} 项需检查': '{{count}} item to inspect',
  '上一周期 {{count}} Token': 'Previous period: {{count}} token',
  '{{count}} 缓存 Token': '{{count}} cached token',
  '{{count}} 次模型调用没有可记录的 Token；当前合计可能偏低。': '{{count}} model call has no trackable token data; the current total may be low.',
  '{{count}} 次模型响应': '{{count}} model response',
  '{{count}} 次工具调用': '{{count}} tool call',
  '{{count}} 次调用': '{{count}} call',
  '模型目录刷新完成，但 {{count}} 个 Provider 拉取失败：{{providers}}': 'Model catalog refresh completed, but {{count}} provider failed to load: {{providers}}',
  '{{count}} 条规则': '{{count}} rule',
  '已配置 {{count}} 条访问规则': '{{count}} access rule configured',
  '{{count}} 个 Bot 实例': '{{count}} Bot instance',
  '{{count}} 个树节点': '{{count}} tree node',
  '{{count}} 个工具调用': '{{count}} tool call',
  '{{count}} 条消息': '{{count}} message',
  '续跑 {{count}} 次': 'Resumed {{count}} time',
  '{{count}} 条结果': '{{count}} result',
  '正在守候 {{count}} 个提醒，最近一个 {{time}}': 'Watching {{count}} reminder; the next is {{time}}',
  '本页 {{count}} 条，可继续向更早的记录回溯。': '{{count}} record on this page; earlier history is available.',
  '本页 {{count}} 条，已到最早记录。': '{{count}} record on this page; this is the earliest history.',
  '{{count}} / 100000 字符': '{{count}} / 100000 character',
  '{{count}} 个空白行': '{{count}} blank line',
  '{{count}} 项能力': '{{count}} capability',
  '{{count}} 行': '{{count}} line',
  '相关发布文件超过 {{count}} 个。': 'There are more than {{count}} relevant release file.',
  '已上传 {{count}} 个产物。': 'Uploaded {{count}} asset.',
  '{{count}} 个版本': '{{count}} version',
  '{{count}} 个产物可上传': '{{count}} asset ready to upload',
  '{{count}} 条记录': '{{count}} record',
  '{{count}} 个任务正在等待': '{{count}} task is waiting',
  '共采样 {{count}} 个 asyncio task；展开条目查看完整快照。': 'Sampled {{count}} asyncio task; expand an item to see the full snapshot.',
  '已保存，{{count}} 项修改已立即生效。': 'Saved. {{count}} change applies now.',
  '已保存，{{count}} 项修改将在安全重启后生效。': 'Saved. {{count}} change applies after a safe restart.',
  '{{count}} 个联系地址': '{{count}} contact address',
  '{{count}} 轮推理': '{{count}} reasoning round',
  '收到了 {{count}} 个附件。': 'Received {{count}} attachment.',
  '收到 {{count}} 张图片。': 'Received {{count}} image.',
  '{{count}} 个连接已接入': '{{count}} connection',
  '与 {{name}} 对话，{{count}} 条未读消息': 'Chat with {{name}}, {{count}} unread message',
  '{{count}} 条新消息等你查看': '{{count}} new message is waiting',
  '已保存 {{count}} 条；修改名字后仍会保留在这个浏览器。': '{{count}} message saved; it remains in this browser after renaming.',
  '{{count}} 条新日志': '{{count}} new log',
};

/** Own-property check that also works with older TypeScript library targets. */
export function hasOwn(object: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(object, key);
}

/** Pick the singular override only for an exact count of one. */
export function selectEnglishPhrase(
  source: string,
  base: string,
  values?: Record<string, string | number>,
): string {
  if (!values || !hasOwn(values, 'count')) return base;
  const count = Number(values.count);
  if (count !== 1) return base;
  return ENGLISH_SINGULAR_OVERRIDES[source] || base;
}
