import type { ComponentType } from 'react';

export type Json = Record<string, any>;

export type AdminRequest = <T = Json>(
  path: string,
  init?: RequestInit,
) => Promise<T>;

export type SettingsPanelProps = {
  value: Json;
  change: (key: string, value: any) => void;
  apply: () => Promise<boolean>;
  dirty: boolean;
  saving: boolean;
  request: AdminRequest;
  secretInputs: Record<string, string>;
  setSecretInputs: (value: Record<string, string>) => void;
  secretStatus: Record<string, { configured?: boolean; last4?: string }>;
};

export type SettingsPanelRegistration = {
  label: string;
  component: ComponentType<SettingsPanelProps>;
};
