import { ChannelAccessSettingsPanel } from './panels/ChannelAccessSettingsPanel';
import { CoworkerSettingsPanel } from './panels/CoworkerSettingsPanel';
import { TelegramSettingsPanel } from './panels/TelegramSettingsPanel';
import { WeComSettingsPanel } from './panels/WeComSettingsPanel';
import { WeixinSettingsPanel } from './panels/WeixinSettingsPanel';
import type { SettingsPanelRegistration } from './types';

const SETTINGS_PANELS: Record<string, SettingsPanelRegistration> = {
  channel_access: {
    label: '信道访问',
    component: ChannelAccessSettingsPanel,
  },
  coworker: {
    label: '搭档互通',
    component: CoworkerSettingsPanel,
  },
  weixin: {
    label: '微信 Claw',
    component: WeixinSettingsPanel,
  },
  telegram: {
    label: 'Telegram',
    component: TelegramSettingsPanel,
  },
  wecom: {
    label: '企业微信',
    component: WeComSettingsPanel,
  },
};

export function settingsPanelRegistration(group: string) {
  return SETTINGS_PANELS[group];
}

export function settingsPanelLabels(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(SETTINGS_PANELS).map(([group, registration]) => [
      group,
      registration.label,
    ]),
  );
}
