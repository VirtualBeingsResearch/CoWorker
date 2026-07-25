import { AlertCircle, ArrowLeft, ArrowRight, CheckCircle2, Circle, FolderOpen, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Field } from "../components/Field";
import { LogOutputLevelControl } from "../components/LogOutputLevelControl";
import { useI18n } from "../i18n";
import type { DictKey } from "../i18n/en";
import {
  approvalsReviewerValues,
  normalizeTimeoutSeconds,
  permissionsModeValues,
  type ApprovalConfigView,
  type ValidationIssue,
} from "../lib/bridgeLogic";
import type {
  ApprovalsReviewer,
  BridgeCoworker,
  CommunicateRegistration,
  ConfigValue,
  PermissionsMode,
} from "../tauri";

export function ConfigView({
  configPath,
  setConfigPath,
  config,
  isDirty,
  fieldError,
  updateConfig,
  updateCodexId,
  desktopUpdateUrlPlaceholder,
  onChooseConfigFile,
  onChooseChatWorkspacesDir,
  approvalConfig,
  updateApprovalConfig,
  isFullAccessDirect,
  isCoworkerReview,
  coworkers,
  selectedIndex,
  selectedCoworker,
  onSelectCoworker,
  updateCoworker,
  onMoveSelectedCoworker,
  onAddCoworker,
  onRemoveSelectedCoworker,
  selectedRegistrations,
  selectedClientId,
  onRefreshRegistrations,
  onRemoveRegistration,
}: {
  configPath: string;
  setConfigPath: (value: string) => void;
  config: ConfigValue;
  isDirty: boolean;
  fieldError: (path: string) => ValidationIssue | undefined;
  updateConfig: (next: ConfigValue) => void;
  updateCodexId: (value: string) => void;
  desktopUpdateUrlPlaceholder: string;
  onChooseConfigFile: () => void;
  onChooseChatWorkspacesDir: () => void;
  approvalConfig: ApprovalConfigView;
  updateApprovalConfig: (next: Partial<ApprovalConfigView>) => void;
  isFullAccessDirect: boolean;
  isCoworkerReview: boolean;
  coworkers: BridgeCoworker[];
  selectedIndex: number;
  selectedCoworker: BridgeCoworker;
  onSelectCoworker: (index: number) => void;
  updateCoworker: (field: keyof BridgeCoworker, value: BridgeCoworker[keyof BridgeCoworker]) => void;
  onMoveSelectedCoworker: (offset: -1 | 1) => void;
  onAddCoworker: () => void;
  onRemoveSelectedCoworker: () => void;
  selectedRegistrations: CommunicateRegistration[];
  selectedClientId: string;
  onRefreshRegistrations: () => void;
  onRemoveRegistration: (registration: CommunicateRegistration) => void;
}) {
  const { t } = useI18n();
  const security = (config.security && typeof config.security === "object" ? config.security : {}) as { development_mode?: boolean };
  const actors = (config.actors && typeof config.actors === "object" ? config.actors : {}) as Record<string, unknown>;
  const codexActor = (actors.codex && typeof actors.codex === "object" ? actors.codex : {}) as Record<string, unknown>;
  const claudeActor = (actors.claude && typeof actors.claude === "object" ? actors.claude : {}) as Record<string, unknown>;
  const enabledCoworkerCount = coworkers.filter((coworker) => coworker.enabled !== false).length;
  const [configPathDraft, setConfigPathDraft] = useState(configPath);

  useEffect(() => {
    setConfigPathDraft(configPath);
  }, [configPath]);

  function applyConfigPath() {
    const nextPath = configPathDraft.trim();
    if (!nextPath || nextPath === configPath || isDirty) return;
    setConfigPath(nextPath);
  }

  function errorMessage(path: string) {
    const issue = fieldError(path);
    return issue ? t(issue.key, issue.vars) : undefined;
  }

  return (
    <section className="panel configPanel">
      <div className="configSection configIdentitySection">
        <div className="sectionHead">
          <div>
            <p className="eyebrow">{t("config.eyebrow")}</p>
            <h3>{t("config.identityTitle")}</h3>
          </div>
          <span className={isDirty ? "dirtyMark active" : "dirtyMark"}>{isDirty ? t("common.unsaved") : t("config.saved")}</span>
        </div>

        <div className="formGrid identityFormGrid">
        <Field label={t("config.fieldDisplayName")} inputId="display-name">
          <input id="display-name" value={config.display_name ?? ""} onChange={(event) => updateConfig({ ...config, display_name: event.target.value })} />
        </Field>
        <Field label={t("common.codexId")} inputId="codex-id" error={errorMessage("codex_id")}>
          <input
            id="codex-id"
            className={errorMessage("codex_id") ? "invalid" : ""}
            value={config.codex_id ?? ""}
            onChange={(event) => updateCodexId(event.target.value)}
          />
        </Field>
        <Field className="fieldSpanFull" label={t("config.fieldConfigPath")} inputId="config-path">
          <div className="pathInputRow">
            <input
              id="config-path"
              value={configPathDraft}
              onChange={(event) => setConfigPathDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") applyConfigPath();
                if (event.key === "Escape") setConfigPathDraft(configPath);
              }}
            />
            <div className="pathInputActions">
              <button
                className="iconButton"
                type="button"
                disabled={isDirty || !configPathDraft.trim() || configPathDraft.trim() === configPath}
                onClick={applyConfigPath}
                aria-label={t("config.applyPath")}
                title={isDirty ? t("config.pathApplyBlocked") : t("config.applyPath")}
              >
                <ArrowRight size={16} aria-hidden="true" />
              </button>
              <button
                className="iconButton"
                type="button"
                disabled={isDirty}
                onClick={onChooseConfigFile}
                aria-label={t("config.chooseConfigFile")}
                title={isDirty ? t("config.pathApplyBlocked") : t("config.chooseConfigFile")}
              >
                <FolderOpen size={16} aria-hidden="true" />
              </button>
            </div>
          </div>
        </Field>
        <Field label={t("config.fieldCodexCommand")} inputId="codex-command">
          <input
            id="codex-command"
            value={String(codexActor.command ?? config.command ?? "")}
            onChange={(event) => updateConfig({
              ...config,
              command: event.target.value,
              actors: { ...actors, codex: { ...codexActor, command: event.target.value } },
            })}
          />
        </Field>
        <Field label={t("config.fieldClaudeCommand")} inputId="claude-command">
          <input
            id="claude-command"
            value={String(claudeActor.command ?? "")}
            onChange={(event) => updateConfig({
              ...config,
              actors: { ...actors, claude: { ...claudeActor, command: event.target.value } },
            })}
          />
        </Field>
        <label className="coworkerEnabledBand fieldSpanFull" htmlFor="close-to-tray">
          <span>
            <strong>{t("config.fieldCloseToTray")}</strong>
            <small>{t("config.hintCloseToTray")}</small>
          </span>
          <span className="toggleControl">
            <input
              id="close-to-tray"
              type="checkbox"
              checked={config.close_to_tray !== false}
              onChange={(event) => updateConfig({ ...config, close_to_tray: event.target.checked })}
            />
            <span aria-hidden="true" />
          </span>
        </label>
        <Field label={t("config.fieldUpdateSubscriptionUrl")} inputId="desktop-update-url" error={errorMessage("desktop_update_url")}>
          <input
            id="desktop-update-url"
            className={errorMessage("desktop_update_url") ? "invalid" : ""}
            value={config.desktop_update_url ?? ""}
            onChange={(event) => updateConfig({ ...config, desktop_update_url: event.target.value })}
            placeholder={desktopUpdateUrlPlaceholder}
          />
          <small className="fieldHint">{t("config.hintUpdateSubscriptionUrl")}</small>
        </Field>
        <Field className="fieldSpanFull" label={t("config.fieldChatWorkspacesDir")} inputId="chat-workspaces-dir">
          <div className="pathInputRow">
            <input
              id="chat-workspaces-dir"
              value={config.chat_workspaces_dir ?? ""}
              onChange={(event) => updateConfig({ ...config, chat_workspaces_dir: event.target.value })}
            />
            <button className="iconButton" type="button" onClick={onChooseChatWorkspacesDir} aria-label={t("aria.chooseChatWorkspacesDir")} title={t("common.chooseFolder")}>
              <FolderOpen size={16} aria-hidden="true" />
            </button>
          </div>
          <small className="fieldHint">{t("config.hintChatWorkspacesDir")}</small>
        </Field>
        <div className="field fieldSpanFull logLevelField">
          <span id="log-output-level-label">{t("config.fieldLogLevel")}</span>
          <LogOutputLevelControl
            value={config.file_log_level ?? config.log_level}
            labelledBy="log-output-level-label"
            onChange={(level) => updateConfig({ ...config, log_level: level, file_log_level: level })}
          />
          <small className="fieldHint">{t("config.hintLogLevel")}</small>
        </div>
        </div>
      </div>

      <div className="configSection coworkerEditor permissionsSection">
        <div className="sectionHead">
          <div>
            <p className="eyebrow">{t("config.eyebrowPermissions")}</p>
            <h3>{t("config.permissionsTitle")}</h3>
          </div>
          <span className={isCoworkerReview ? "statePill" : "dirtyMark"}>
            {isCoworkerReview ? t("config.reviewCoworker") : t("config.reviewLocalPolicy")}
          </span>
        </div>

        <label className="permissionModeBand" htmlFor="development-mode">
          <span className="permissionModeCopy">
            <strong>{t("config.fieldDevelopmentMode")}</strong>
            <small>{t("config.developmentModeHint")}</small>
          </span>
          <span className="toggleControl">
              <input
                id="development-mode"
                type="checkbox"
                checked={security.development_mode === true}
                onChange={(event) => updateConfig({
                  ...config,
                  security: { ...security, development_mode: event.target.checked },
                })}
              />
            <span aria-hidden="true" />
          </span>
        </label>

        <div className="permissionsControls">
          <Field label={t("config.fieldPermissionsMode")} inputId="permissions-mode">
            <select
              id="permissions-mode"
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

          <Field label={t("config.fieldApprovalsReviewer")} inputId="approvals-reviewer">
            <select
              id="approvals-reviewer"
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

          <Field label={t("config.fieldApprovalTimeout")} inputId="approval-timeout-seconds">
            <input
              id="approval-timeout-seconds"
              min={0}
              step={1}
              type="number"
              value={approvalConfig.approvalTimeoutSeconds}
              onChange={(event) =>
                updateApprovalConfig({
                  approvalTimeoutSeconds: normalizeTimeoutSeconds(event.target.value),
                })
              }
            />
            <small className="fieldHint">{t("config.hintApprovalTimeout")}</small>
          </Field>
        </div>

        {isFullAccessDirect && (
          <div className="riskNotice" role="alert">
            <AlertCircle size={16} />
            <p>{t("config.riskFullAccessDirect")}</p>
          </div>
        )}
      </div>

      <div className="configSection coworkerEditor coworkersSection">
        <div className="sectionHead">
          <div>
            <p className="eyebrow">{t("config.eyebrowCoworkers")}</p>
            <h3>{t("config.coworkersTitle")}</h3>
          </div>
          <div className="inlineActions">
            <button
              className="iconButton"
              onClick={() => onMoveSelectedCoworker(-1)}
              disabled={selectedIndex === 0}
              title={t("config.moveCoworkerEarlier")}
              aria-label={t("config.moveCoworkerEarlier")}
              type="button"
            >
              <ArrowLeft size={16} />
            </button>
            <button
              className="iconButton"
              onClick={() => onMoveSelectedCoworker(1)}
              disabled={selectedIndex === coworkers.length - 1}
              title={t("config.moveCoworkerLater")}
              aria-label={t("config.moveCoworkerLater")}
              type="button"
            >
              <ArrowRight size={16} />
            </button>
            <button className="softButton" onClick={onAddCoworker}>
              <Plus size={16} /> {t("common.add")}
            </button>
            <button className="dangerButton" onClick={onRemoveSelectedCoworker} disabled={coworkers.length <= 1}>
              <Trash2 size={16} /> {t("common.delete")}
            </button>
          </div>
        </div>

        <div className="coworkerTabs" role="tablist" aria-label={t("config.coworkersTitle")}>
          {coworkers.map((coworker, index) => (
            <button
              key={`${index}-${coworker.coworker_id}`}
              className={index === selectedIndex ? "active" : ""}
              onClick={() => onSelectCoworker(index)}
              role="tab"
              aria-selected={index === selectedIndex}
            >
              {coworker.display_name || coworker.coworker_id}
              {coworker.enabled === false && <span className="disabledProfileBadge">{t("config.coworkerDisabled")}</span>}
            </button>
          ))}
        </div>

        <div className="formGrid">
          <label className="coworkerEnabledBand fieldSpanFull" htmlFor="coworker-enabled">
            <span>
              <strong>{t("config.fieldCoworkerEnabled")}</strong>
              <small>{t("config.hintCoworkerEnabled")}</small>
            </span>
            <span className="toggleControl">
              <input
                id="coworker-enabled"
                type="checkbox"
                checked={selectedCoworker.enabled !== false}
                disabled={selectedCoworker.enabled !== false && enabledCoworkerCount <= 1}
                onChange={(event) => updateCoworker("enabled", event.target.checked)}
              />
              <span aria-hidden="true" />
            </span>
          </label>
          <Field label={t("config.fieldCoworkerId")} inputId="coworker-id" error={errorMessage(`coworkers.${selectedIndex}.coworker_id`)}>
            <input
              id="coworker-id"
              className={errorMessage(`coworkers.${selectedIndex}.coworker_id`) ? "invalid" : ""}
              value={selectedCoworker.coworker_id}
              onChange={(event) => updateCoworker("coworker_id", event.target.value)}
            />
          </Field>
          <Field label={t("config.fieldCoworkerName")} inputId="coworker-name">
            <input id="coworker-name" value={selectedCoworker.display_name} onChange={(event) => updateCoworker("display_name", event.target.value)} />
          </Field>
          <Field label={t("config.fieldCoworkerBaseUrl")} inputId="coworker-url" error={errorMessage(`coworkers.${selectedIndex}.base_url`)}>
            <input
              id="coworker-url"
              className={errorMessage(`coworkers.${selectedIndex}.base_url`) ? "invalid" : ""}
              value={selectedCoworker.base_url}
              onChange={(event) => updateCoworker("base_url", event.target.value)}
            />
          </Field>
          <Field label={t("config.fieldBearerToken")} inputId="coworker-token">
            <input
              id="coworker-token"
              type="password"
              autoComplete="off"
              value={selectedCoworker.bearer_token ?? ""}
              onChange={(event) => updateCoworker("bearer_token", event.target.value)}
            />
          </Field>
        </div>

        <div className="registrySubsection">
        <div className="sectionHead">
          <div>
            <p className="eyebrow">{t("config.eyebrowServerRegistry")}</p>
            <h3>{t("config.registryTitle")}</h3>
          </div>
          <button className="softButton" onClick={onRefreshRegistrations}>
            <RefreshCw size={16} /> {t("common.refresh")}
          </button>
        </div>
        <div className="diagnostics">
          {selectedRegistrations.length ? (
            selectedRegistrations.map((registration) => (
              <div className={`diag ${registration.active ? "ok" : "pending"}`} key={registration.registration_id}>
                {registration.active ? <CheckCircle2 size={16} /> : <Circle size={12} />}
                <div>
                  <strong>{registration.participant_id}</strong>
                  <p>{registration.active ? t("common.active") : t("config.lastRegisteredAt", { time: registration.last_registered_at || t("common.unknown") })}</p>
                </div>
                <button className="dangerButton" onClick={() => onRemoveRegistration(registration)} disabled={registration.active}>
                  <Trash2 size={14} /> {t("common.delete")}
                </button>
              </div>
            ))
          ) : (
            <div className="diag pending">
              <Circle size={12} />
              <div>
                <strong>{t("config.noRegistrationsLoaded")}</strong>
                <p>{selectedClientId}</p>
              </div>
            </div>
          )}
        </div>
        </div>
      </div>
    </section>
  );
}
