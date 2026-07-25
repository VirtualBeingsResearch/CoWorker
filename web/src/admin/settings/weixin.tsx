import { QrCode, RefreshCw, Trash2, TriangleAlert, UserRound, MessagesSquare } from 'lucide-react';
import { useCallback, useRef, useState } from 'react';

import { t } from '../../i18n/admin';
import type { Json, SettingsPanelProps } from './types';

export function WeixinSettingsPanel({ value, change, onApplied, request }: SettingsPanelProps) {
  const accounts = Array.isArray(value.accounts) ? value.accounts : [];
  const [qrImage, setQrImage] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [loginStatus, setLoginStatus] = useState('');
  const [loginError, setLoginError] = useState('');
  const [verifyCode, setVerifyCode] = useState('');
  const [starting, setStarting] = useState(false);
  const pollingRef = useRef(false);
  const patchAccount = (id: string, patch: Json) => change(
    'accounts',
    accounts.map((account: Json) => account.id === id ? { ...account, ...patch } : account),
  );
  const removeAccount = (account: Json) => {
    if (!confirm(t('移除 ClawBot 连接“{{name}}”？该连接会立即停止收发，但微信侧授权不会被远程注销。', { name: account.name || account.bot_id }))) return;
    change('accounts', accounts.filter((item: Json) => item.id !== account.id));
  };
  const poll = useCallback(async (activeSession: string, code = '') => {
    if (pollingRef.current || !activeSession) return;
    pollingRef.current = true;
    try {
      const result = await request<Json>('/api/admin/channels/weixin/login/poll', {
        method: 'POST',
        body: JSON.stringify({ session_id: activeSession, verify_code: code }),
      });
      setLoginStatus(result.status || 'wait');
      if (result.status === 'confirmed') {
        setQrImage(''); setSessionId(''); setVerifyCode('');
        await onApplied();
        return;
      }
      if (!['expired', 'verify_code_blocked', 'binded_redirect'].includes(result.status)) {
        window.setTimeout(() => void poll(activeSession), 900);
      }
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : t('微信连接状态读取失败'));
    } finally {
      pollingRef.current = false;
    }
  }, [onApplied, request]);
  const startLogin = async () => {
    setStarting(true); setLoginError(''); setLoginStatus('wait'); setVerifyCode('');
    try {
      const result = await request<Json>('/api/admin/channels/weixin/login/start', { method: 'POST' });
      setQrImage(result.qrcode_data_url || '');
      setSessionId(result.session_id || '');
      window.setTimeout(() => void poll(result.session_id || ''), 200);
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : t('无法生成微信连接二维码'));
    } finally {
      setStarting(false);
    }
  };
  const submitVerifyCode = () => {
    if (verifyCode.trim()) void poll(sessionId, verifyCode.trim());
  };
  return <div className="weixin-settings">
    <section className={'weixin-overview ' + (value.enabled ? accounts.length ? 'ready' : 'warning' : 'disabled')}>
      <div><MessagesSquare size={23} /><span><small>{t('个人微信 ClawBot')}</small><b>{value.enabled ? accounts.length ? t('{{count}} 个连接已接入', { count: accounts.length }) : t('等待添加首个连接') : t('微信 Claw 已停用')}</b><p>{t('ClawBot 是实例级信道连接；微信联系人首次发消息后产生独立 participant。')}</p></span></div>
      <label className="switch"><input type="checkbox" checked={!!value.enabled} onChange={event => change('enabled', event.target.checked)} /><i /><span>{t('启用微信 Claw')}</span></label>
      <button className="primary" disabled={starting} onClick={() => void startLogin()}><QrCode size={15} />{t(starting ? '正在生成…' : accounts.length ? '添加 ClawBot 连接' : '扫码连接微信')}</button>
    </section>
    {(qrImage || loginError) && <section className="weixin-pairing">
      <div className="weixin-qr-stage">{qrImage ? <img src={qrImage} alt={t('微信 Claw 连接二维码')} /> : <TriangleAlert size={28} />}</div>
      <div className="weixin-pairing-copy"><span>{t('临时连接凭证')}</span><h3>{t(loginStatus === 'scaned' ? '已扫码，请在手机上确认' : loginStatus === 'need_verifycode' ? '输入手机显示的数字' : '使用微信扫描二维码')}</h3><p>{loginError || t('扫码只会新增 ClawBot 信道连接；不会与生成二维码或接收二维码的 participant 建立绑定。')}</p>
        {loginStatus === 'need_verifycode' && <div className="weixin-verify"><input autoFocus value={verifyCode} onChange={event => setVerifyCode(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') submitVerifyCode(); }} placeholder={t('手机上显示的数字')} /><button className="primary mini" onClick={submitVerifyCode}>{t('继续连接')}</button></div>}
        <button className="ghost mini" onClick={() => void startLogin()}><RefreshCw size={13} />{t('刷新二维码')}</button>
      </div>
    </section>}
    <div className="weixin-account-list">{accounts.length ? accounts.map((account: Json) => <article key={account.id}>
      <div className="weixin-account-mark"><UserRound size={18} /></div>
      <div className="weixin-account-copy"><input aria-label={t('连接名称')} value={account.name || ''} onChange={event => patchAccount(account.id, { name: event.target.value })} placeholder={t('给这个 ClawBot 连接命名')} /><code>{account.bot_id}</code><small>{account.weixin_user_id ? t('登录微信 ID：{{id}}', { id: account.weixin_user_id }) : t('微信账号标识未返回')}</small></div>
      <label className="switch"><input type="checkbox" checked={account.enabled !== false} onChange={event => patchAccount(account.id, { enabled: event.target.checked })} /><i /><span>{t(account.enabled !== false ? '在线' : '停用')}</span></label>
      <button className="danger-icon" onClick={() => removeAccount(account)} title={t('移除连接')}><Trash2 size={15} /></button>
    </article>) : <div className="provider-empty">{t('还没有 ClawBot 连接。点击“扫码连接微信”添加第一个连接。')}</div>}</div>
  </div>;
}
