import { ChannelAccessSettingsPanel } from './panels/ChannelAccessSettingsPanel';
import { ModelApiSettingsPanel } from './panels/ModelApiSettingsPanel';
import { TelegramSettingsPanel } from './panels/TelegramSettingsPanel';
import { WeComSettingsPanel } from './panels/WeComSettingsPanel';
import { WeixinSettingsPanel } from './panels/WeixinSettingsPanel';
import type { SettingsPanelRegistration } from './types';

const SETTINGS_PANELS: Record<string, SettingsPanelRegistration> = {
  channel_access: {
    label: '信道访问',
    component: ChannelAccessSettingsPanel,
  },
  model_api: {
    label: '模型接口',
    component: ModelApiSettingsPanel,
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
