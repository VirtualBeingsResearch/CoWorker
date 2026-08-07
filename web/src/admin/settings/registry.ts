import { ChannelAccessSettingsPanel } from './panels/ChannelAccessSettingsPanel';
import { WeixinSettingsPanel } from './panels/WeixinSettingsPanel';
import type { SettingsPanelRegistration } from './types';

const SETTINGS_PANELS: Record<string, SettingsPanelRegistration> = {
  channel_access: {
    label: '信道访问',
    component: ChannelAccessSettingsPanel,
  },
  weixin: {
    label: '微信 Claw',
    component: WeixinSettingsPanel,
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
