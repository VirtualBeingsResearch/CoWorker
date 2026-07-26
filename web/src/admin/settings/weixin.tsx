import { MessagesSquare, QrCode, RefreshCw, Trash2, TriangleAlert, UserRound } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { t } from '../../i18n/admin';
import type { Json, SettingsPanelProps } from './types';

const TERMINAL_PAIRING_STATUSES = new Set([
  'confirmed',
  'expired',
  'verify_code_blocked',
  'binded_redirect',
]);

export function WeixinSettingsPanel({ value, change, request }: SettingsPanelProps) {
  const [connections, setConnections] = useState<Json[]>([]);
  const [qrImage, setQrImage] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [loginStatus, setLoginStatus] = useState('');
  const [loginError, setLoginError] = useState('');
  const [verifyCode, setVerifyCode] = useState('');
  const [starting, setStarting] = useState(false);
  const pollTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  const loadConnections = useCallback(async () => {
    const result = await request<Json>('/api/admin/channels/weixin/management');
    if (mountedRef.current) {
      setConnections(Array.isArray(result.connections) ? result.connections : []);
    }
  }, [request]);

  const readPairing = useCallback(async () => {
    try {
      const result = await request<Json>('/api/admin/channels/weixin/management');
      if (!mountedRef.current) return;
      const session = result.pairing;
      if (!session) return;
      setSessionId(session.session_id || '');
      setLoginStatus(session.status || 'wait');
      if (session.qrcode_data_url) setQrImage(session.qrcode_data_url);
      if (session.status === 'confirmed') {
        setQrImage('');
        setVerifyCode('');
        await loadConnections();
        return;
      }
      if (!TERMINAL_PAIRING_STATUSES.has(session.status)) {
        pollTimerRef.current = window.setTimeout(() => void readPairing(), 900);
      }
    } catch (error) {
      if (mountedRef.current) {
        setLoginError(error instanceof Error ? error.message : t('微信连接状态读取失败'));
      }
    }
  }, [loadConnections, request]);

  useEffect(() => {
    mountedRef.current = true;
    void loadConnections();
    void readPairing();
    return () => {
      mountedRef.current = false;
      if (pollTimerRef.current !== null) window.clearTimeout(pollTimerRef.current);
    };
  }, [loadConnections, readPairing]);

  const startLogin = async () => {
    setStarting(true);
    setLoginError('');
    setLoginStatus('wait');
    setVerifyCode('');
    try {
      const result = await request<Json>('/api/admin/channels/weixin/management/start_pairing', {
        method: 'POST',
        body: '{}',
      });
      setQrImage(result.qrcode_data_url || '');
      setSessionId(result.session_id || '');
      if (pollTimerRef.current !== null) window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = window.setTimeout(() => void readPairing(), 200);
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : t('无法生成微信连接二维码'));
    } finally {
      setStarting(false);
    }
  };

  const submitVerifyCode = async () => {
    if (!verifyCode.trim()) return;
    try {
      await request<Json>('/api/admin/channels/weixin/management/verify_pairing', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId,
          verify_code: verifyCode.trim(),
        }),
      });
      void readPairing();
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : t('微信连接状态读取失败'));
    }
  };

  const updateConnection = async (botInstanceId: string, patch: Json) => {
    await request<Json>(
      '/api/admin/channels/weixin/management/update_connection',
      {
        method: 'POST',
        body: JSON.stringify({ bot_instance_id: botInstanceId, ...patch }),
      },
    );
    await loadConnections();
  };

  const removeConnection = async (connection: Json) => {
    const name = connection.display_name || connection.bot_instance_id;
    if (!confirm(t('移除 ClawBot 连接“{{name}}”？该连接将停止收发，但微信侧授权不会被远程注销。', { name }))) return;
    await request<Json>(
      '/api/admin/channels/weixin/management/remove_connection',
      {
        method: 'POST',
        body: JSON.stringify({
          bot_instance_id: connection.bot_instance_id,
          confirm: true,
        }),
      },
    );
    await loadConnections();
  };

  return <div className="weixin-settings">
    <section className={'weixin-overview ' + (value.enabled ? connections.length ? 'ready' : 'warning' : 'disabled')}>
      <div><MessagesSquare size={23} /><span><small>{t('个人微信 ClawBot')}</small><b>{value.enabled ? connections.length ? t('{{count}} 个连接已接入', { count: connections.length }) : t('等待添加首个连接') : t('微信 Claw 已停用')}</b><p>{t('一个 ClawBot 实例绑定一个微信账号，并对应一个独立 participant。')}</p></span></div>
      <label className="switch"><input type="checkbox" checked={!!value.enabled} onChange={event => change('enabled', event.target.checked)} /><i /><span>{t('启用微信 Claw')}</span></label>
      <button className="primary" disabled={starting} onClick={() => void startLogin()}><QrCode size={15} />{t(starting ? '正在生成…' : connections.length ? '添加 ClawBot 连接' : '扫码连接微信')}</button>
    </section>
    {(qrImage || loginError) && <section className="weixin-pairing">
      <div className="weixin-qr-stage">{qrImage ? <img src={qrImage} alt={t('微信 Claw 连接二维码')} /> : <TriangleAlert size={28} />}</div>
      <div className="weixin-pairing-copy"><span>{t('临时连接凭证')}</span><h3>{t(loginStatus === 'scaned' ? '已扫码，请在手机上确认' : loginStatus === 'need_verifycode' ? '输入手机显示的数字' : '使用微信扫描二维码')}</h3><p>{loginError || t('扫码会新增一个独立 ClawBot 实例；二维码由谁查看不会改变连接归属。')}</p>
        {loginStatus === 'need_verifycode' && <div className="weixin-verify"><input autoFocus value={verifyCode} onChange={event => setVerifyCode(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void submitVerifyCode(); }} placeholder={t('手机上显示的数字')} /><button className="primary mini" onClick={() => void submitVerifyCode()}>{t('继续连接')}</button></div>}
        <button className="ghost mini" onClick={() => void startLogin()}><RefreshCw size={13} />{t('刷新二维码')}</button>
      </div>
    </section>}
    <div className="weixin-account-list">{connections.length ? connections.map((connection: Json) => <article key={connection.bot_instance_id}>
      <div className="weixin-account-mark"><UserRound size={18} /></div>
      <div className="weixin-account-copy"><input aria-label={t('连接名称')} defaultValue={connection.display_name || ''} onBlur={event => { if (event.target.value !== (connection.display_name || '')) void updateConnection(connection.bot_instance_id, { display_name: event.target.value }); }} placeholder={t('给这个 ClawBot 连接命名')} /><code>{connection.bot_instance_id}</code><small>{connection.weixin_user_id ? t('登录微信 ID：{{id}}', { id: connection.weixin_user_id }) : t('微信账号标识未返回')}</small></div>
      <label className="switch"><input type="checkbox" checked={connection.enabled !== false} onChange={event => void updateConnection(connection.bot_instance_id, { enabled: event.target.checked })} /><i /><span>{t(connection.enabled !== false ? '在线' : '停用')}</span></label>
      <button className="danger-icon" onClick={() => void removeConnection(connection)} title={t('移除连接')}><Trash2 size={15} /></button>
    </article>) : <div className="provider-empty">{t('还没有 ClawBot 连接。点击“扫码连接微信”添加第一个连接。')}</div>}</div>
  </div>;
}
