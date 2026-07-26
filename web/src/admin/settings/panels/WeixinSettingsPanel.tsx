import { CircleCheckBig, MessagesSquare, QrCode, RefreshCw, Trash2, TriangleAlert, UserRound } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { t } from '../../../i18n/admin';
import type { Json, SettingsPanelProps } from '../types';

type PairingPresentation = {
  title: string;
  detail: string;
  terminal?: boolean;
  failed?: boolean;
};

const MANAGEMENT_PATH = '/api/admin/channels/weixin/management';
const PAIRING_POLL_MS = 900;
const PAIRING_START_POLL_MS = 200;
const DEFAULT_PAIRING_PRESENTATION: PairingPresentation = {
  title: '使用微信扫描二维码',
  detail: '扫码会新增一个独立 ClawBot 实例；二维码由谁查看不会改变连接归属。',
};
const PAIRING_PRESENTATIONS: Record<string, PairingPresentation> = {
  scaned: {
    title: '已扫码，请在手机上确认',
    detail: '二维码已被识别，连接会在手机确认后自动建立。',
  },
  need_verifycode: {
    title: '输入手机显示的数字',
    detail: '完成验证后会继续建立 ClawBot 连接。',
  },
  confirmed: {
    title: '微信连接已建立',
    detail: '新的 ClawBot 实例已经加入连接列表。',
    terminal: true,
  },
  expired: {
    title: '二维码已过期',
    detail: '重新生成二维码后再使用微信扫描。',
    terminal: true,
    failed: true,
  },
  verify_code_blocked: {
    title: '验证次数暂时受限',
    detail: '请稍后重新生成二维码并再次连接。',
    terminal: true,
    failed: true,
  },
  binded_redirect: {
    title: '这个微信账号已经绑定其他 Bot',
    detail: 'iLink 只允许一个微信账号绑定一个 Bot 实例。',
    terminal: true,
    failed: true,
  },
};

function pairingPresentation(status: string) {
  const presentation = PAIRING_PRESENTATIONS[status] || DEFAULT_PAIRING_PRESENTATION;
  return {
    ...presentation,
    title: t(presentation.title),
    detail: t(presentation.detail),
  };
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : t(fallback);
}

export function WeixinSettingsPanel({ value, change, apply, dirty, saving, request }: SettingsPanelProps) {
  const [connections, setConnections] = useState<Json[]>([]);
  const [qrImage, setQrImage] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [loginStatus, setLoginStatus] = useState('');
  const [loginError, setLoginError] = useState('');
  const [managementError, setManagementError] = useState('');
  const [verifyCode, setVerifyCode] = useState('');
  const [starting, setStarting] = useState(false);
  const pollTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  const loadConnections = useCallback(async () => {
    try {
      const result = await request<Json>(MANAGEMENT_PATH);
      if (mountedRef.current) {
        setConnections(Array.isArray(result.connections) ? result.connections : []);
        setManagementError('');
      }
    } catch (error) {
      if (mountedRef.current) {
        setManagementError(errorMessage(error, '微信连接状态读取失败'));
      }
    }
  }, [request]);

  const readPairing = useCallback(async () => {
    try {
      const result = await request<Json>(MANAGEMENT_PATH);
      if (!mountedRef.current) return;
      const session = result.pairing;
      if (!session) return;
      setSessionId(session.session_id || '');
      setLoginStatus(session.status || 'wait');
      const presentation = pairingPresentation(session.status);
      if (presentation.failed) setQrImage('');
      else if (session.qrcode_data_url) setQrImage(session.qrcode_data_url);
      if (session.status === 'confirmed') {
        setQrImage('');
        setVerifyCode('');
        await loadConnections();
        return;
      }
      if (!presentation.terminal) {
        pollTimerRef.current = window.setTimeout(() => void readPairing(), PAIRING_POLL_MS);
      }
    } catch (error) {
      if (mountedRef.current) {
        setLoginError(errorMessage(error, '微信连接状态读取失败'));
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
    if (!value.enabled) return;
    setStarting(true);
    setLoginError('');
    setLoginStatus('wait');
    setVerifyCode('');
    try {
      if (dirty && !await apply()) return;
      const result = await request<Json>(`${MANAGEMENT_PATH}/start_pairing`, {
        method: 'POST',
        body: '{}',
      });
      setQrImage(result.qrcode_data_url || '');
      setSessionId(result.session_id || '');
      setLoginStatus(result.status || 'wait');
      if (pollTimerRef.current !== null) window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = window.setTimeout(
        () => void readPairing(),
        PAIRING_START_POLL_MS,
      );
    } catch (error) {
      setLoginError(errorMessage(error, '无法生成微信连接二维码'));
    } finally {
      setStarting(false);
    }
  };

  const submitVerifyCode = async () => {
    if (!verifyCode.trim()) return;
    try {
      await request<Json>(`${MANAGEMENT_PATH}/verify_pairing`, {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId,
          verify_code: verifyCode.trim(),
        }),
      });
      void readPairing();
    } catch (error) {
      setLoginError(errorMessage(error, '微信连接状态读取失败'));
    }
  };

  const updateConnection = async (botInstanceId: string, patch: Json) => {
    try {
      setManagementError('');
      await request<Json>(
        `${MANAGEMENT_PATH}/update_connection`,
        {
          method: 'POST',
          body: JSON.stringify({ bot_instance_id: botInstanceId, ...patch }),
        },
      );
      await loadConnections();
    } catch (error) {
      setManagementError(errorMessage(error, '微信连接更新失败'));
    }
  };

  const removeConnection = async (connection: Json) => {
    const name = connection.display_name || connection.bot_instance_id;
    if (!confirm(t('移除 ClawBot 连接“{{name}}”？该连接将停止收发，但微信侧授权不会被远程注销。', { name }))) return;
    try {
      setManagementError('');
      await request<Json>(
        `${MANAGEMENT_PATH}/remove_connection`,
        {
          method: 'POST',
          body: JSON.stringify({
            bot_instance_id: connection.bot_instance_id,
            confirm: true,
          }),
        },
      );
      await loadConnections();
    } catch (error) {
      setManagementError(errorMessage(error, '微信连接移除失败'));
    }
  };

  const presentation = pairingPresentation(loginStatus);
  const showPairing = Boolean(
    qrImage || loginError || loginStatus === 'confirmed' || presentation.failed,
  );
  return <div className="weixin-settings">
    <section className={'weixin-overview ' + (value.enabled ? connections.length ? 'ready' : 'warning' : 'disabled')}>
      <div><MessagesSquare size={23} /><span><small>{t('个人微信 ClawBot')}</small><b>{value.enabled ? connections.length ? t('{{count}} 个连接已接入', { count: connections.length }) : t('等待添加首个连接') : t('微信 Claw 已停用')}</b><p>{t('一个 ClawBot 实例绑定一个微信账号，并对应一个独立 participant。')}</p></span></div>
      <label className="switch"><input type="checkbox" checked={!!value.enabled} onChange={event => change('enabled', event.target.checked)} /><i /><span>{t('启用微信 Claw')}</span></label>
      <button className="primary" disabled={starting || saving || !value.enabled} onClick={() => void startLogin()}><QrCode size={15} />{t(starting || saving ? '正在准备连接…' : !value.enabled ? '启用后扫码连接' : connections.length ? '添加 ClawBot 连接' : '扫码连接微信')}</button>
    </section>
    {showPairing && <section className={'weixin-pairing ' + (loginStatus === 'confirmed' ? 'confirmed' : presentation.failed || loginError ? 'failed' : '')}>
      <div className="weixin-qr-stage">{qrImage ? <img src={qrImage} alt={t('微信 Claw 连接二维码')} /> : loginStatus === 'confirmed' ? <CircleCheckBig size={31} /> : <TriangleAlert size={28} />}</div>
      <div className="weixin-pairing-copy"><span>{t('连接状态')}</span><h3>{presentation.title}</h3><p>{loginError || presentation.detail}</p>
        {loginStatus === 'need_verifycode' && <div className="weixin-verify"><input autoFocus value={verifyCode} onChange={event => setVerifyCode(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void submitVerifyCode(); }} placeholder={t('手机上显示的数字')} /><button className="primary mini" onClick={() => void submitVerifyCode()}>{t('继续连接')}</button></div>}
        {loginStatus !== 'confirmed' && <button className="ghost mini" disabled={!value.enabled || starting || saving} onClick={() => void startLogin()}><RefreshCw size={13} />{t(qrImage ? '刷新二维码' : '重新生成二维码')}</button>}
      </div>
    </section>}
    {managementError && <div className="notice error" role="alert">{managementError}</div>}
    <div className="weixin-account-list">{connections.length ? connections.map((connection: Json) => <article key={connection.bot_instance_id}>
      <div className="weixin-account-mark"><UserRound size={18} /></div>
      <div className="weixin-account-copy"><input aria-label={t('连接名称')} defaultValue={connection.display_name || ''} onBlur={event => { if (event.target.value !== (connection.display_name || '')) void updateConnection(connection.bot_instance_id, { display_name: event.target.value }); }} placeholder={t('给这个 ClawBot 连接命名')} /><code>{connection.bot_instance_id}</code><small>{connection.weixin_user_id ? t('登录微信 ID：{{id}}', { id: connection.weixin_user_id }) : t('微信账号标识未返回')}</small></div>
      <label className="switch"><input type="checkbox" checked={connection.enabled !== false} onChange={event => void updateConnection(connection.bot_instance_id, { enabled: event.target.checked })} /><i /><span>{t(connection.enabled !== false ? '在线' : '停用')}</span></label>
      <button className="danger-icon" onClick={() => void removeConnection(connection)} title={t('移除连接')}><Trash2 size={15} /></button>
    </article>) : <div className="provider-empty">{t('还没有 ClawBot 连接。点击“扫码连接微信”添加第一个连接。')}</div>}</div>
  </div>;
}
