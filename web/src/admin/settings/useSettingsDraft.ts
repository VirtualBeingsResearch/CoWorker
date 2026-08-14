import { useCallback, useEffect, useMemo, useState } from 'react';

import { t } from '../../i18n/admin';
import { validateModelPrices } from './modelPricing';
import type { AdminRequest, Json } from './types';

type SaveMessage = {
  kind: 'success' | 'error';
  text: string;
};

type SettingsDraftOptions = {
  serverData: Json | null;
  updateServerData: (data: Json) => void;
  request: AdminRequest;
  describeDesktopSave: (before: Json, after: Json, fallback: string) => string;
};

export function useSettingsDraft({
  serverData,
  updateServerData,
  request,
  describeDesktopSave,
}: SettingsDraftOptions) {
  const [draft, setDraft] = useState<Json | null>(null);
  const [group, setGroup] = useState(
    () => new URLSearchParams(window.location.search).get('group') || 'llm',
  );
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({});
  const [clearOverridePaths, setClearOverridePaths] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<SaveMessage | null>(null);
  const [desktopValidationError, setDesktopValidationError] = useState('');
  const [invalidJsonPaths, setInvalidJsonPaths] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (serverData) setDraft(current => current || structuredClone(serverData.config));
  }, [serverData]);

  const dirtyGroups = useMemo(
    () => collectDirtyGroups(serverData, draft, secretInputs, clearOverridePaths),
    [clearOverridePaths, draft, secretInputs, serverData],
  );

  useEffect(() => {
    const warnAboutUnsavedChanges = (event: BeforeUnloadEvent) => {
      if (dirtyGroups.size > 0) event.preventDefault();
    };
    window.addEventListener('beforeunload', warnAboutUnsavedChanges);
    return () => window.removeEventListener('beforeunload', warnAboutUnsavedChanges);
  }, [dirtyGroups]);

  useEffect(() => {
    const syncGroupFromLocation = () => {
      if (new URLSearchParams(window.location.search).get('section') !== 'settings') return;
      setGroup(new URLSearchParams(window.location.search).get('group') || 'llm');
      setInvalidJsonPaths(new Set());
      setMessage(null);
    };
    window.addEventListener('popstate', syncGroupFromLocation);
    return () => window.removeEventListener('popstate', syncGroupFromLocation);
  }, []);

  const setJsonValidity = useCallback((path: string, valid: boolean) => {
    setInvalidJsonPaths(current => {
      const next = new Set(current);
      if (valid) next.delete(path);
      else next.add(path);
      return setsEqual(current, next) ? current : next;
    });
  }, []);

  const change = useCallback((key: string, value: unknown) => {
    setClearOverridePaths(current => withoutValue(current, `${group}.${key}`));
    setDraft(current => (
      current
        ? { ...current, [group]: { ...current[group], [key]: value } }
        : current
    ));
  }, [group]);

  const selectGroup = useCallback((nextGroup: string) => {
    setGroup(nextGroup);
    setInvalidJsonPaths(new Set());
    setMessage(null);
    const params = new URLSearchParams(window.location.search);
    params.set('section', 'settings');
    params.set('group', nextGroup);
    if (nextGroup !== 'desktop_updates') params.delete('source');
    window.history.pushState(null, '', `?${params.toString()}`);
  }, []);

  const changeProvider = useCallback((index: number, key: string, value: unknown) => {
    if (!draft) return;
    const providers = [...(draft.llm?.managed_providers || [])];
    providers[index] = { ...providers[index], [key]: value };
    change('managed_providers', providers);
  }, [change, draft]);

  const toggleClearOverride = useCallback((path: string) => {
    setClearOverridePaths(current => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const resetGroup = useCallback(() => {
    if (!serverData) return;
    setDraft(current => (
      current
        ? { ...current, [group]: structuredClone(serverData.config[group] || {}) }
        : current
    ));
    setSecretInputs(current => omitGroupEntries(current, group));
    setClearOverridePaths(current => omitGroupPaths(current, group));
    setInvalidJsonPaths(new Set());
    setMessage(null);
  }, [group, serverData]);

  const save = useCallback(async (): Promise<boolean> => {
    if (!serverData || !draft) return false;
    const validationMessage = settingsValidationMessage(
      group,
      draft,
      desktopValidationError,
      invalidJsonPaths,
    );
    if (validationMessage) {
      setMessage({ kind: 'error', text: validationMessage });
      return false;
    }
    if (!dirtyGroups.has(group)) return true;

    const beforeGroup = structuredClone(serverData.config?.[group] || {});
    const afterGroup = structuredClone(draft[group] || {});
    setSaving(true);
    setMessage(null);
    try {
      const result = await request<Json>('/api/admin/config', {
        method: 'PATCH',
        body: JSON.stringify(buildConfigPatch(
          group,
          beforeGroup,
          afterGroup,
          secretInputs,
          clearOverridePaths,
        )),
      });
      const savedMessage = configSavedMessage(result);
      setSecretInputs(current => omitGroupEntries(current, group));
      setClearOverridePaths(current => omitGroupPaths(current, group));
      setMessage({
        kind: 'success',
        text: group === 'desktop_updates'
          ? describeDesktopSave(beforeGroup, afterGroup, savedMessage)
          : savedMessage,
      });
      const refreshed = await request<Json>('/api/admin/config');
      updateServerData(refreshed);
      setDraft(current => (
        current
          ? { ...current, [group]: structuredClone(refreshed.config[group] || {}) }
          : current
      ));
      return true;
    } catch (saveError) {
      setMessage({
        kind: 'error',
        text: saveError instanceof Error ? saveError.message : t('保存失败'),
      });
      return false;
    } finally {
      setSaving(false);
    }
  }, [
    clearOverridePaths,
    desktopValidationError,
    describeDesktopSave,
    dirtyGroups,
    draft,
    group,
    invalidJsonPaths,
    request,
    secretInputs,
    serverData,
    updateServerData,
  ]);

  const isHot = useCallback(
    (path: string) => (serverData?.hot_reloadable || []).some(
      (item: string) => path === item || path.startsWith(`${item}.`),
    ),
    [serverData],
  );

  return {
    change,
    changeProvider,
    clearOverridePaths,
    desktopValidationError,
    dirtyGroups,
    draft,
    group,
    invalidJsonPaths,
    isHot,
    message,
    resetGroup,
    save,
    saving,
    secretInputs,
    selectGroup,
    setDesktopValidationError,
    setJsonValidity,
    setSecretInputs,
    toggleClearOverride,
  };
}

function collectDirtyGroups(
  serverData: Json | null,
  draft: Json | null,
  secretInputs: Record<string, string>,
  clearOverridePaths: Set<string>,
) {
  const dirty = new Set<string>();
  if (!serverData || !draft) return dirty;
  for (const key of Object.keys(draft)) {
    if (Object.keys(changedConfigFields(
      serverData.config?.[key] || {},
      draft[key] || {},
    )).length > 0) {
      dirty.add(key);
    }
  }
  for (const [path, value] of Object.entries(secretInputs)) {
    if (value) dirty.add(path.split('.')[0]);
  }
  for (const path of clearOverridePaths) dirty.add(path.split('.')[0]);
  return dirty;
}

function settingsValidationMessage(
  group: string,
  draft: Json,
  desktopValidationError: string,
  invalidJsonPaths: Set<string>,
) {
  if (group === 'desktop_updates' && desktopValidationError) {
    return desktopValidationError;
  }
  if (invalidJsonPaths.size > 0) return t('请先修正标出的 JSON 格式。');
  const maxTokens = Number(draft.llm?.max_tokens);
  if (group === 'llm' && (!Number.isInteger(maxTokens) || maxTokens <= 0)) {
    return t('单次输出上限必须是正整数。');
  }
  if (group === 'llm') {
    for (const provider of draft.llm?.managed_providers || []) {
      const models = Array.isArray(provider.model_capabilities) ? provider.model_capabilities : [];
      const ids = models.map((item: Json) => String(item.model || '').trim());
      if (ids.some((model: string) => !model)) return t('模型能力声明必须填写模型 ID。');
      if (new Set(ids).size !== ids.length) return t('同一个 Provider 中不能重复声明模型能力。');
      if (models.some((item: Json) => item.video && !item.vision)) return t('支持视频的模型也必须支持图片理解。');
    }
    const pricingError = validateModelPrices(draft.llm?.model_prices);
    if (pricingError === 'identity') return t('模型定价必须填写 Provider 和模型 ID。');
    if (pricingError === 'currency') return t('模型定价币种必须是三个英文字母。');
    if (pricingError === 'rates') return t('输入价和输出价必须是非负数。');
    if (pricingError === 'cached_rate') return t('缓存输入价必须留空或填写非负数。');
    if (pricingError === 'duplicate') return t('同一个 Provider 中不能重复配置模型价格。');
  }
  return '';
}

function buildConfigPatch(
  group: string,
  before: Json,
  after: Json,
  secretInputs: Record<string, string>,
  clearOverridePaths: Set<string>,
) {
  const changedFields = changedConfigFields(before, after);
  return {
    changes: Object.keys(changedFields).length > 0 ? { [group]: changedFields } : {},
    secrets: Object.fromEntries(
      Object.entries(secretInputs).filter(
        ([path, value]) => path.startsWith(`${group}.`) && value !== '',
      ),
    ),
    clear_overrides: [...clearOverridePaths].filter(path => path.startsWith(`${group}.`)),
  };
}

function configSavedMessage(result: Json) {
  const hotCount = result.applied_now?.length || 0;
  const restartCount = result.requires_restart?.length || 0;
  if (hotCount && restartCount) {
    return t('已保存：{{hot}} 项立即生效，{{restart}} 项等待重启。', {
      hot: hotCount,
      restart: restartCount,
    });
  }
  if (hotCount) return t('已保存，{{count}} 项修改已立即生效。', { count: hotCount });
  if (restartCount) {
    return t('已保存，{{count}} 项修改将在安全重启后生效。', {
      count: restartCount,
    });
  }
  return t('配置没有变化。');
}

function changedConfigFields(before: Json, after: Json): Json {
  return Object.fromEntries(
    Object.entries(after).filter(
      ([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key]),
    ),
  );
}

function omitGroupEntries(
  entries: Record<string, string>,
  group: string,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(entries).filter(([path]) => !path.startsWith(`${group}.`)),
  );
}

function omitGroupPaths(paths: Set<string>, group: string): Set<string> {
  return new Set([...paths].filter(path => !path.startsWith(`${group}.`)));
}

function withoutValue(values: Set<string>, value: string): Set<string> {
  if (!values.has(value)) return values;
  const next = new Set(values);
  next.delete(value);
  return next;
}

function setsEqual(left: Set<string>, right: Set<string>) {
  return left.size === right.size && [...left].every(item => right.has(item));
}
