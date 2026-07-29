import { ArrowLeft, ArrowRight, Check, FolderOpen, MessagesSquare, Play, ScrollText, Settings2, Sparkles, X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Field } from "./Field";
import { useI18n } from "../i18n";
import type { DictKey } from "../i18n/en";
import {
  approvalsReviewerValues,
  isRelayBaseUrl,
  permissionsModeValues,
  type ApprovalConfigView,
  type ValidationIssue,
} from "../lib/bridgeLogic";
import type { ApprovalsReviewer, BridgeCoworker, ConfigValue, PermissionsMode } from "../tauri";

type Step = {
  icon: ReactNode;
  titleKey: DictKey;
  descKey: DictKey;
};

const STEPS: Step[] = [
  { icon: <Sparkles size={20} />, titleKey: "onboarding.step.1.title", descKey: "onboarding.step.1.desc" },
  { icon: <Settings2 size={20} />, titleKey: "onboarding.step.2.title", descKey: "onboarding.step.2.desc" },
  { icon: <MessagesSquare size={20} />, titleKey: "onboarding.step.3.title", descKey: "onboarding.step.3.desc" },
  { icon: <Play size={20} />, titleKey: "onboarding.step.4.title", descKey: "onboarding.step.4.desc" },
  { icon: <Check size={20} />, titleKey: "onboarding.step.5.title", descKey: "onboarding.step.5.desc" },
];

export function OnboardingWizard({
  open,
  onClose,
  onFinished,
  config,
  selectedCoworker,
  selectedIndex,
  approvalConfig,
  fieldError,
  updateConfig,
  updateCodexId,
  updateCoworker,
  updateApprovalConfig,
  onChooseChatWorkspacesDir,
  coworkers,
  desktopUpdateUrlPlaceholder,
  issuesCount,
  onSave,
  onSaveAndStart,
}: {
  open: boolean;
  onClose: () => void;
  onFinished: (reachedEnd: boolean) => void;
  config: ConfigValue;
  selectedCoworker: BridgeCoworker;
  selectedIndex: number;
  approvalConfig: ApprovalConfigView;
  fieldError: (path: string) => ValidationIssue | undefined;
  updateConfig: (next: ConfigValue) => void;
  updateCodexId: (value: string) => void;
  updateCoworker: (field: keyof BridgeCoworker, value: BridgeCoworker[keyof BridgeCoworker]) => void;
  updateApprovalConfig: (next: Partial<ApprovalConfigView>) => void;
  onChooseChatWorkspacesDir: () => void;
  coworkers: BridgeCoworker[];
  desktopUpdateUrlPlaceholder: string;
  issuesCount: number;
  onSave: () => Promise<void> | void;
  onSaveAndStart: () => Promise<void> | void;
}) {
  const { t } = useI18n();
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState<null | "save" | "start">(null);
  // Track whether the user manually edited the update URL, and the last base URL
  // it was auto-synced from, so following the Coworker address stays the default
  // until the user overrides it.
  const updateUrlManuallyEditedRef = useRef(false);
  const lastSyncedBaseUrlRef = useRef(selectedCoworker.base_url);

  // Restart from the first step every time the wizard is (re)opened.
  useEffect(() => {
    if (open) {
      setStep(0);
      setSaving(null);
      updateUrlManuallyEditedRef.current = false;
      const coworkerBaseUrl = selectedCoworker.base_url.trim();
      const desktopUpdateUrl = String(config.desktop_update_url ?? "").trim();
      const packagedDefaultUrl = desktopUpdateUrlPlaceholder.trim();
      lastSyncedBaseUrlRef.current = coworkerBaseUrl;
      if (coworkerBaseUrl && (!desktopUpdateUrl || desktopUpdateUrl === packagedDefaultUrl)) {
        updateConfig({ ...config, desktop_update_url: coworkerBaseUrl });
      }
    }
  }, [open]);

  // Escape closes the wizard (treated as skipping setup).
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const actors = (config.actors && typeof config.actors === "object" && !Array.isArray(config.actors) ? config.actors : {}) as Record<string, unknown>;
  const codexActor = (actors.codex && typeof actors.codex === "object" ? actors.codex : {}) as Record<string, unknown>;
  const claudeActor = (actors.claude && typeof actors.claude === "object" ? actors.claude : {}) as Record<string, unknown>;

  function errorMessage(path: string) {
    const issue = fieldError(path);
    return issue ? t(issue.key, issue.vars) : undefined;
  }

  function handleBaseUrlChange(value: string) {
    const nextCoworkers = coworkers.map((coworker, index) =>
      index === selectedIndex ? { ...coworker, base_url: value } : coworker,
    );
    const trimmed = value.trim();
    const currentUpdateUrl = String(config.desktop_update_url ?? "").trim();
    const defaultUrl = desktopUpdateUrlPlaceholder.trim();
    const nextConfig: ConfigValue = { ...config, coworkers: nextCoworkers };
    // Default the desktop update URL to the Coworker address until the user edits it manually.
    if (
      trimmed
      && !updateUrlManuallyEditedRef.current
      && (currentUpdateUrl === "" || currentUpdateUrl === defaultUrl || currentUpdateUrl === lastSyncedBaseUrlRef.current.trim())
    ) {
      nextConfig.desktop_update_url = trimmed;
      lastSyncedBaseUrlRef.current = trimmed;
    }
    updateConfig(nextConfig);
  }

  function handleUpdateUrlChange(value: string) {
    updateUrlManuallyEditedRef.current = true;
    updateConfig({ ...config, desktop_update_url: value });
  }

  const total = STEPS.length;
  const current = STEPS[step];
  const isLast = step === total - 1;
  const canFinish = issuesCount === 0;
  const busy = saving !== null;
  const progressLabel = t("onboarding.progress", { current: step + 1, total });

  async function finish(andStart: boolean) {
    setSaving(andStart ? "start" : "save");
    try {
      if (andStart) {
        await onSaveAndStart();
      } else {
        await onSave();
      }
      onFinished(true);
    } catch {
      // App surfaces errors via inlineNotice; re-enable the buttons.
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="modalBackdrop" role="presentation" onClick={onClose}>
      <div
        className="onboardingDialog"
        role="dialog"
        aria-modal="true"
        aria-label={t("aria.onboarding")}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="onboardingHeader">
          <div>
            <p className="eyebrow">{t("onboarding.eyebrow")}</p>
            <h3>{t("onboarding.title")}</h3>
          </div>
          <button className="iconButton" onClick={onClose} aria-label={t("onboarding.skip")} type="button">
            <X size={15} />
          </button>
        </div>

        <div className="onboardingProgress" aria-label={progressLabel}>
          {STEPS.map((_, index) => (
            <span key={index} className={index === step ? "onboardingDot active" : "onboardingDot"} aria-hidden="true" />
          ))}
          <small>{progressLabel}</small>
        </div>

        <div className="onboardingStep" key={step}>
          <span className="onboardingStepIcon" aria-hidden="true">{current.icon}</span>
          <h4>{t(current.titleKey)}</h4>
          <p>{t(current.descKey)}</p>

          {step === 1 && (
            <div className="onboardingForm">
              <Field label={t("common.codexId")} inputId="onboarding-codex-id" error={errorMessage("codex_id")}>
                <input
                  id="onboarding-codex-id"
                  className={errorMessage("codex_id") ? "invalid" : ""}
                  value={config.codex_id ?? ""}
                  onChange={(event) => updateCodexId(event.target.value)}
                />
              </Field>
              <Field label={t("config.fieldDisplayName")} inputId="onboarding-display-name">
                <input
                  id="onboarding-display-name"
                  value={config.display_name ?? ""}
                  onChange={(event) => updateConfig({ ...config, display_name: event.target.value })}
                />
              </Field>
              <Field label={t("config.fieldCodexCommand")} inputId="onboarding-codex-command">
                <input
                  id="onboarding-codex-command"
                  value={String(codexActor.command ?? config.command ?? "")}
                  onChange={(event) => updateConfig({
                    ...config,
                    command: event.target.value,
                    actors: { ...actors, codex: { ...codexActor, command: event.target.value } },
                  })}
                />
              </Field>
              <Field label={t("config.fieldClaudeCommand")} inputId="onboarding-claude-command">
                <input
                  id="onboarding-claude-command"
                  value={String(claudeActor.command ?? "")}
                  onChange={(event) => updateConfig({
                    ...config,
                    actors: { ...actors, claude: { ...claudeActor, command: event.target.value } },
                  })}
                />
              </Field>
              <Field label={t("config.fieldChatWorkspacesDir")} inputId="onboarding-chat-workspaces-dir">
                <div className="pathInputRow">
                  <input
                    id="onboarding-chat-workspaces-dir"
                    value={config.chat_workspaces_dir ?? ""}
                    onChange={(event) => updateConfig({ ...config, chat_workspaces_dir: event.target.value })}
                  />
                  <button
                    className="iconButton"
                    type="button"
                    onClick={onChooseChatWorkspacesDir}
                    aria-label={t("aria.chooseChatWorkspacesDir")}
                    title={t("common.chooseFolder")}
                  >
                    <FolderOpen size={16} aria-hidden="true" />
                  </button>
                </div>
              </Field>
            </div>
          )}

          {step === 2 && (
            <div className="onboardingForm">
              <Field label={t("config.fieldCoworkerName")} inputId="onboarding-coworker-name">
                <input
                  id="onboarding-coworker-name"
                  value={selectedCoworker.display_name}
                  onChange={(event) => updateCoworker("display_name", event.target.value)}
                />
              </Field>
              <Field
                label={t("config.fieldCoworkerBaseUrl")}
                inputId="onboarding-coworker-url"
                error={errorMessage(`coworkers.${selectedIndex}.base_url`)}
              >
                <input
                  id="onboarding-coworker-url"
                  className={errorMessage(`coworkers.${selectedIndex}.base_url`) ? "invalid" : ""}
                  value={selectedCoworker.base_url}
                  onChange={(event) => handleBaseUrlChange(event.target.value)}
                />
                {isRelayBaseUrl(selectedCoworker.base_url) && (
                  <small className="fieldHint">{t("config.relayE2eeDetected")}</small>
                )}
              </Field>
              <Field
                label={t("config.fieldUpdateSubscriptionUrl")}
                inputId="onboarding-desktop-update-url"
                error={errorMessage("desktop_update_url")}
              >
                <input
                  id="onboarding-desktop-update-url"
                  className={errorMessage("desktop_update_url") ? "invalid" : ""}
                  value={config.desktop_update_url ?? ""}
                  placeholder={selectedCoworker.base_url.trim() || desktopUpdateUrlPlaceholder}
                  onChange={(event) => handleUpdateUrlChange(event.target.value)}
                />
                <small className="fieldHint">{t("config.hintUpdateSubscriptionUrl")}</small>
              </Field>
              <Field label={t("config.fieldBearerToken")} inputId="onboarding-coworker-token">
                <input
                  id="onboarding-coworker-token"
                  type="password"
                  autoComplete="off"
                  value={selectedCoworker.bearer_token ?? ""}
                  onChange={(event) => updateCoworker("bearer_token", event.target.value)}
                />
              </Field>
            </div>
          )}

          {step === 3 && (
            <div className="onboardingForm">
              <Field label={t("config.fieldPermissionsMode")} inputId="onboarding-permissions-mode">
                <select
                  id="onboarding-permissions-mode"
                  value={approvalConfig.permissionsMode}
                  onChange={(event) => updateApprovalConfig({ permissionsMode: event.target.value as PermissionsMode })}
                >
                  {permissionsModeValues.map((value) => (
                    <option key={value} value={value}>
                      {t(`permissions.mode.${value}.label` as DictKey)}
                    </option>
                  ))}
                </select>
                <small className="fieldHint">{t(`permissions.mode.${approvalConfig.permissionsMode}.desc` as DictKey)}</small>
              </Field>
              <Field label={t("config.fieldApprovalsReviewer")} inputId="onboarding-approvals-reviewer">
                <select
                  id="onboarding-approvals-reviewer"
                  value={approvalConfig.approvalsReviewer}
                  onChange={(event) => updateApprovalConfig({ approvalsReviewer: event.target.value as ApprovalsReviewer })}
                >
                  {approvalsReviewerValues.map((value) => (
                    <option key={value} value={value}>
                      {t(`permissions.reviewer.${value}.label` as DictKey)}
                    </option>
                  ))}
                </select>
                <small className="fieldHint">{t(`permissions.reviewer.${approvalConfig.approvalsReviewer}.desc` as DictKey)}</small>
              </Field>
            </div>
          )}

          {step === 4 && (
            <div className="onboardingForm onboardingSummary">
              <div className="onboardingSummaryRow">
                <span>{t("common.codexId")}</span>
                <strong>{config.codex_id || t("common.notConfigured")}</strong>
              </div>
              <div className="onboardingSummaryRow">
                <span>{t("config.fieldCoworkerBaseUrl")}</span>
                <strong>{selectedCoworker.base_url || t("common.notConfigured")}</strong>
              </div>
              <div className="onboardingSummaryRow">
                <span>{t("config.fieldPermissionsMode")}</span>
                <strong>{t(`permissions.mode.${approvalConfig.permissionsMode}.label` as DictKey)}</strong>
              </div>
              {issuesCount > 0 && (
                <div className="notice notice-warning" role="status">
                  <span>{t("onboarding.validationBlocked", { count: issuesCount })}</span>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="onboardingActions">
          <button className="softButton" onClick={() => onFinished(false)} type="button">
            {t("onboarding.skip")}
          </button>
          <div className="onboardingNav">
            <button
              className="softButton"
              onClick={() => setStep((value) => Math.max(0, value - 1))}
              disabled={step === 0}
              type="button"
            >
              <ArrowLeft size={15} /> {t("onboarding.prev")}
            </button>
            {isLast ? (
              <>
                <button
                  className="primaryAction"
                  onClick={() => finish(false)}
                  disabled={!canFinish || busy}
                  type="button"
                >
                  {t("onboarding.save")}
                </button>
                <button
                  className="primaryAction"
                  onClick={() => finish(true)}
                  disabled={!canFinish || busy}
                  type="button"
                >
                  <Play size={14} /> {t("onboarding.saveAndStart")}
                </button>
              </>
            ) : (
              <button
                className="primaryAction"
                onClick={() => setStep((value) => Math.min(total - 1, value + 1))}
                type="button"
              >
                {t("onboarding.next")} <ArrowRight size={15} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
