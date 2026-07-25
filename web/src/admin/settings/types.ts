export type Json = Record<string, any>;

export type AdminRequest = <T = Json>(
  path: string,
  init?: RequestInit,
) => Promise<T>;

export type SettingsPanelProps = {
  value: Json;
  change: (key: string, value: any) => void;
  onApplied: () => Promise<void>;
  request: AdminRequest;
};

export type SettingsPanelRegistration = {
  label: string;
  component: ComponentType<SettingsPanelProps>;
};
import type { ComponentType } from 'react';
