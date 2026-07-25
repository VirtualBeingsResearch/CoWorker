import { ArrowRight, PanelTopClose, Power, Rows3 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";

export function CloseToTrayNotice({
  open,
  onChoose,
}: {
  open: boolean;
  onChoose: (keepRunning: boolean) => Promise<void>;
}) {
  const { t } = useI18n();
  const [choiceInProgress, setChoiceInProgress] = useState<"tray" | "exit" | null>(null);
  const trayChoiceRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) trayChoiceRef.current?.focus();
  }, [open]);

  if (!open) return null;

  async function choose(keepRunning: boolean) {
    setChoiceInProgress(keepRunning ? "tray" : "exit");
    try {
      await onChoose(keepRunning);
    } finally {
      setChoiceInProgress(null);
    }
  }

  return (
    <div className="modalBackdrop closeToTrayBackdrop" role="presentation">
      <div
        className="closeToTrayDialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="close-to-tray-title"
        aria-describedby="close-to-tray-description"
      >
        <div className="closeToTrayRoute" aria-hidden="true">
          <span className="closeToTrayWindow"><PanelTopClose size={19} /></span>
          <span className="closeToTrayLine" />
          <ArrowRight size={15} />
          <span className="closeToTrayDestinations">
            <span className="closeToTrayDock"><i /><i /><i /></span>
            <span className="closeToTrayExit"><Power size={15} /></span>
          </span>
        </div>
        <div className="closeToTrayCopy">
          <p className="eyebrow">{t("closeToTrayNotice.eyebrow")}</p>
          <h3 id="close-to-tray-title">{t("closeToTrayNotice.title")}</h3>
          <p id="close-to-tray-description">{t("closeToTrayNotice.description")}</p>
        </div>
        <div className="closeToTrayActions">
          <button
            className="softButton"
            disabled={choiceInProgress !== null}
            onClick={() => void choose(false)}
            type="button"
          >
            <Power size={14} />
            {choiceInProgress === "exit"
              ? t("closeToTrayNotice.exiting")
              : t("closeToTrayNotice.exit")}
          </button>
          <button
            className="softButton closeToTrayPrimary"
            disabled={choiceInProgress !== null}
            onClick={() => void choose(true)}
            ref={trayChoiceRef}
            type="button"
          >
            <Rows3 size={14} />
            {choiceInProgress === "tray"
              ? t("closeToTrayNotice.hiding")
              : t("closeToTrayNotice.keepRunning")}
          </button>
        </div>
      </div>
    </div>
  );
}
