import { Mapping, RemapMode, VP_TARGET_GROUPS } from "../types";

interface Props {
  visible: boolean;
  mode: RemapMode;
  selectedButton: string | null;
  currentFwMapping: Mapping | undefined;
  currentVpTarget: string | null;
  remapping: boolean;
  onFirmwareAssign: (target: string) => void;
  onFirmwareUnmap: () => void;
  onVPAssign: (buttonId: string, targetCode: string) => void;
  onClose: () => void;
}

// ── Firmware helpers ─────────────────────────────────────────

const FACE  = [{ short: "Y", display: "Y", pos: "top" }, { short: "X", display: "X", pos: "left" },
               { short: "B", display: "B", pos: "right" }, { short: "A", display: "A", pos: "bot" }];
const DPAD  = [{ short: "UP", display: "↑", pos: "top" }, { short: "LEFT", display: "←", pos: "left" },
               { short: "RIGHT", display: "→", pos: "right" }, { short: "DOWN", display: "↓", pos: "bot" }];
const BUMPERS  = [{ short: "LB", display: "LB" }, { short: "RB", display: "RB" }];
const TRIGGERS = [{ short: "LT", display: "LT" }, { short: "RT", display: "RT" }];
const STICKS   = [{ short: "L3", display: "L3" }, { short: "R3", display: "R3" }];

export default function AssignPanel({
  visible, mode, selectedButton,
  currentFwMapping, currentVpTarget,
  remapping, onFirmwareAssign, onFirmwareUnmap, onVPAssign, onClose,
}: Props) {

  // ── Shared button renderer ───────────────────────────────────
  function fwBtn(short: string, display: string) {
    const isCurrent = currentFwMapping?.target_short === short;
    return (
      <button key={short} className={`ap-btn ${isCurrent ? "current" : ""}`}
        onClick={() => onFirmwareAssign(short)} disabled={remapping} title={short}>
        {display}
      </button>
    );
  }

  function vpBtn(label: string, code: string) {
    const isCurrent = currentVpTarget === label;
    return (
      <button key={code} className={`ap-btn ${isCurrent ? "current" : ""}`}
        onClick={() => selectedButton && onVPAssign(selectedButton, code)}
        disabled={remapping} title={label}>
        {label}
      </button>
    );
  }

  function diamond(items: typeof FACE, isFw: boolean) {
    const m = Object.fromEntries(items.map(i => [i.pos, i]));
    return (
      <div className="ap-diamond">
        <div className="ap-diamond-row">
          {m.top && (isFw ? fwBtn(m.top.short, m.top.display) : vpBtn(m.top.display, ""))}
        </div>
        <div className="ap-diamond-row ap-diamond-mid">
          {m.left  && (isFw ? fwBtn(m.left.short,  m.left.display)  : vpBtn(m.left.display,  ""))}
          <div className="ap-diamond-gap" />
          {m.right && (isFw ? fwBtn(m.right.short, m.right.display) : vpBtn(m.right.display, ""))}
        </div>
        <div className="ap-diamond-row">
          {m.bot  && (isFw ? fwBtn(m.bot.short,  m.bot.display)  : vpBtn(m.bot.display,  ""))}
        </div>
      </div>
    );
  }

  // ── Source preview box ───────────────────────────────────────
  const currentDisplay = mode === "firmware"
    ? currentFwMapping?.target_short ?? null
    : currentVpTarget;

  return (
    <aside className={`assign-panel ${visible ? "visible" : ""}`}>
      <div className="assign-panel-inner">
        <div className="assign-panel-header">
          <div>
            <span className="assign-panel-title">Mapping Type</span>
            <span className={`assign-panel-mode-tag ${mode}`}>
              {mode === "firmware" ? "Firmware" : "Virtual Pad"}
            </span>
          </div>
          <button className="assign-panel-close" onClick={onClose}>×</button>
        </div>

        <div className="ap-body">
          {/* ── Target picker (left) ── */}
          <div className="ap-targets">
            {mode === "firmware" ? (
              <>
                <div className="ap-section-label">ABXY</div>
                {diamond(FACE, true)}
                <div className="ap-divider" />
                <div className="ap-section-label">D-Pad</div>
                {diamond(DPAD, true)}
                <div className="ap-divider" />
                <div className="ap-row-group">
                  <div className="ap-section-label">Bumpers</div>
                  <div className="ap-row">{BUMPERS.map(b => fwBtn(b.short, b.display))}</div>
                </div>
                <div className="ap-row-group">
                  <div className="ap-section-label">Triggers</div>
                  <div className="ap-row">{TRIGGERS.map(b => fwBtn(b.short, b.display))}</div>
                </div>
                <div className="ap-row-group">
                  <div className="ap-section-label">Sticks</div>
                  <div className="ap-row">{STICKS.map(b => fwBtn(b.short, b.display))}</div>
                </div>
              </>
            ) : (
              VP_TARGET_GROUPS.map(group => (
                <div key={group.label} className="ap-row-group">
                  <div className="ap-section-label">{group.label}</div>
                  <div className="ap-row">
                    {group.targets.map(t => vpBtn(t.label, t.code))}
                  </div>
                </div>
              ))
            )}
          </div>

          {/* ── Source preview (right) ── */}
          <div className="ap-preview">
            <div className="ap-section-label">Button</div>
            <div className="ap-source-box">
              <span className="ap-source-label">{selectedButton ?? "—"}</span>
              <span className="ap-source-hint">
                {currentDisplay ? `→ ${currentDisplay}` : "unassigned"}
              </span>
            </div>
            {mode === "firmware" && currentFwMapping && (
              <button
                className="ap-clear-btn"
                onClick={onFirmwareUnmap}
                disabled={remapping}
                title="Remove this mapping"
              >
                Clear
              </button>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
