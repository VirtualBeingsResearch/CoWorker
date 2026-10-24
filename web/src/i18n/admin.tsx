import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Languages } from 'lucide-react';

import { setDateTimeLocale } from '../lib/dateTime';
import { ENGLISH_PHRASES, STATUS_ENGLISH_PHRASES } from './catalog';
import { hasOwn, selectEnglishPhrase } from './plural';

export type AdminLanguage = 'zh' | 'en';
export type LanguageSurface = 'admin' | 'status';

type AdminI18nValue = {
  language: AdminLanguage;
  setLanguage: (language: AdminLanguage) => void;
};

const STORAGE_KEY = 'coworker-language';
const LEGACY_STORAGE_KEY = 'coworker-admin-language';
const I18nContext = createContext<AdminI18nValue | null>(null);

/*
 * English copy lives in catalog.ts. The admin console predates the desktop
 * application's keyed dictionaries, so Chinese source strings remain stable
 * migration identifiers while React chooses the visible language during
 * render. API values and user-authored content are intentionally untouched.
 */
let activeLanguage: AdminLanguage = 'zh';
let activeSurface: LanguageSurface = 'admin';
const CJK_SOURCE = /[\u3400-\u9fff]/;
const reportedMissingPhrases = new Set<string>();

function detectInitialLanguage(): AdminLanguage {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY) || window.localStorage.getItem(LEGACY_STORAGE_KEY);
    if (saved === 'zh' || saved === 'en') return saved;
  } catch {
    // Storage can be unavailable in privacy-constrained browser contexts.
  }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en';
}

function interpolate(template: string, values?: Record<string, string | number>) {
  if (!values) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (match, key) => hasOwn(values, key) ? String(values[key]) : match);
}

/**
 * Translation entry point for the legacy-sized admin surface. Components call
 * this while rendering; no DOM post-processing is used, so React remains the
 * source of truth for copy, accessibility labels, and updates.
 *
 * Chinese source strings are stable keys so the migration can stay incremental.
 * English count phrases keep their plural form in the catalogs; the singular
 * override is selected only when `count` is exactly one.
 */
export function t(source: string, values?: Record<string, string | number>) {
  const primary = activeSurface === 'admin' ? ENGLISH_PHRASES : STATUS_ENGLISH_PHRASES;
  const secondary = activeSurface === 'admin' ? STATUS_ENGLISH_PHRASES : ENGLISH_PHRASES;
  const english = hasOwn(primary, source)
    ? primary[source]
    : hasOwn(secondary, source)
      ? secondary[source]
      : undefined;
  if (activeLanguage === 'en' && english === undefined && CJK_SOURCE.test(source) && import.meta.env.DEV && !reportedMissingPhrases.has(source)) {
    reportedMissingPhrases.add(source);
    console.warn(`[i18n] Missing English phrase: ${source}`);
  }
  const message = activeLanguage === 'en' && english !== undefined
    ? selectEnglishPhrase(source, english, values)
    : source;
  return interpolate(message, values);
}

export function LanguageProvider({
  children,
  surface = 'admin',
}: {
  children: ReactNode;
  surface?: LanguageSurface;
}) {
  const [language, setLanguageState] = useState<AdminLanguage>(detectInitialLanguage);
  activeLanguage = language;
  activeSurface = surface;
  setDateTimeLocale(language === 'zh' ? 'zh-CN' : 'en-US');
  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    document.title = language === 'zh'
      ? (surface === 'admin' ? '照看室' : '搭档状态')
      : (surface === 'admin' ? 'Care Station' : 'Coworker Status');
  }, [language, surface]);
  const value = useMemo<AdminI18nValue>(() => ({
    language,
    setLanguage: (nextLanguage: AdminLanguage) => {
      try { window.localStorage.setItem(STORAGE_KEY, nextLanguage); } catch { /* Storage is optional. */ }
      setLanguageState(nextLanguage);
    },
  }), [language]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function AdminLanguageProvider({ children }: { children: ReactNode }) {
  return <LanguageProvider surface="admin">{children}</LanguageProvider>;
}

export function StatusLanguageProvider({ children }: { children: ReactNode }) {
  return <LanguageProvider surface="status">{children}</LanguageProvider>;
}

export function useAdminI18n() {
  const value = useContext(I18nContext);
  if (!value) throw new Error('useAdminI18n must be used inside LanguageProvider');
  return value;
}

export function LanguageSwitch({ className = '' }: { className?: string }) {
  const { language, setLanguage } = useAdminI18n();
  const nextLanguage = language === 'zh' ? 'en' : 'zh';
  const nextLanguageLabel = language === 'zh' ? 'English' : '中文';
  const label = language === 'zh' ? '切换界面语言为 English' : 'Switch interface language to Chinese';
  return <button type="button" className={className.trim()} aria-label={label} title={label} onClick={() => setLanguage(nextLanguage)}>
    <Languages size={15} aria-hidden="true" />
    <span>{nextLanguageLabel}</span>
  </button>;
}

export function AdminLanguageSwitch({ className = '' }: { className?: string }) {
  return <LanguageSwitch className={('admin-language-toggle ' + className).trim()} />;
}
