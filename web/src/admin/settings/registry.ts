import { WeixinSettingsPanel } from './panels/WeixinSettingsPanel';
import type { SettingsPanelRegistration } from './types';

const SETTINGS_PANELS: Record<string, SettingsPanelRegistration> = {
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
