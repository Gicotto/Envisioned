import { useState } from "react";
import controllerImg from "../assets/controller.png";
import type { Mapping, RemapMode, VirtualPadConfig } from "../types";
import { VP_HOTSPOTS, VP_SOURCE_CODES } from "../types";
import type { LiveInput } from "../App";

// Firmware hotspot positions (user-tuned)
const FW_HOTSPOTS = [
  { id: "S1", xPct: 13.5,  yPct: 20    },
  { id: "S2", xPct: 52.55, yPct: 20    },
  { id: "P1", xPct: 59.75, yPct: 64    },
  { id: "P2", xPct: 64.0,  yPct: 55    },
  { id: "P3", xPct: 82.0,  yPct: 55    },
  { id: "P4", xPct: 86.29, yPct: 64.15 },
];

// VP hotspot positions live in types.ts (VP_HOTSPOTS) — edit there.

const INFO_TEXT = [
  "Firmware remapping writes directly to the controller chip. Changes persist even when unplugged and apply to back paddles (P1–P4) and side buttons (S1–S2) only.",
  "Virtual Pad remapping changes how the Linux virtual controller reports buttons to games. Applies to face buttons, bumpers, and stick clicks. Requires the virtual pad bridge to be running.",
];

interface Props {
  mode: RemapMode;
  onModeChange: (m: RemapMode) => void;
  selectedButton: string | null;
  onSelectButton: (id: string) => void;
  mappings: Mapping[];
  vpConfig: VirtualPadConfig | null;
  loading: boolean;
  flashMap?: Record<string, number>;
  liveInput?: LiveInput | null;
  eventLog?: string[];
  onRetryMonitor?: () => void;
}

export default function ControllerView({
  mode, onModeChange, selectedButton, onSelectButton,
  mappings, vpConfig, loading, flashMap, liveInput,
  eventLog, onRetryMonitor,
}: Props) {
  const [showInfo, setShowInfo] = useState(false);

  const fwAssigned = new Set(mappings.map(m => m.source_short));
  const vpAssigned = new Set(
    vpConfig
      ? Object.entries(VP_SOURCE_CODES)
          .filter(([, srcCode]) => vpConfig.button_remap[srcCode] !== undefined)
          .map(([id]) => id)
      : []
  );

  function renderHotspot(id: string, xPct: number, yPct: number, variant: "fw" | "vp", pill?: boolean, rotate?: number, opaque?: boolean, transparent?: boolean, size?: number, noLabel?: boolean, width?: number, height?: number, borderWidth?: number) {
    const isSelected = selectedButton === id;
    const isActive   = mode === "both"
      || (variant === "fw" ? mode === "firmware" : mode === "vpad");
    const isAssigned = variant === "fw" ? fwAssigned.has(id) : vpAssigned.has(id);
    const flashN     = flashMap?.[id] ?? 0;

    return (
      <button
        key={`${id}-${flashN}`}
        className={[
          "hotspot",
          `hotspot--${variant}`,
          pill        ? "hotspot--pill"        : "",
          opaque      ? "hotspot--opaque"      : "",
          transparent ? "hotspot--transparent" : "",
          isSelected  ? "selected"  : "",
          isActive    ? "hs-active" : "hs-dimmed",
          isAssigned  ? "assigned"  : "",
          flashN > 0  ? "flashing"  : "",
        ].filter(Boolean).join(" ")}
        style={{ left: `${xPct}%`, top: `${yPct}%`, transform: `translate(-50%, -50%) rotate(${rotate ?? 0}deg)`, ...(width ? { width } : size ? { width: size } : {}), ...(height ? { height } : size ? { height: size } : {}), ...(borderWidth ? { borderWidth } : {}) }}
        onClick={() => onSelectButton(id)}
        title={id}
      >
        <span className="hotspot-plus">{isSelected ? "●" : "+"}</span>
        {!noLabel && <span className="hotspot-label">{id}</span>}
      </button>
    );
  }

  return (
    <div className="controller-view">
      {/* Mode toggle row */}
      <div className="mode-toggle-row">
        <button
          className={`mode-btn ${mode === "firmware" ? "active" : ""}`}
          onClick={() => onModeChange("firmware")}
        >
          Firmware
        </button>
        <button
          className={`mode-btn ${mode === "vpad" ? "active" : ""}`}
          onClick={() => onModeChange("vpad")}
        >
          Virtual Pad
        </button>
        <button
          className={`mode-btn ${mode === "both" ? "active" : ""}`}
          onClick={() => onModeChange("both")}
        >
          Both
        </button>
        <div className="info-wrap">
          <button className="info-btn" onClick={() => setShowInfo(v => !v)} title="What's the difference?">
            ℹ
          </button>
          {showInfo && (
            <div className="info-popup">
              <button className="info-popup-close" onClick={() => setShowInfo(false)}>×</button>
              {INFO_TEXT.map((p, i) => <p key={i}>{p}</p>)}
            </div>
          )}
        </div>
      </div>

      {loading ? (
        <div className="controller-loading">
          <span className="spinner" /> Reading device…
        </div>
      ) : (
        <div className="controller-img-wrap">
          <img
            src={controllerImg}
            alt="SCUF Envision Pro"
            className="controller-img"
            draggable={false}
          />
          {FW_HOTSPOTS.map(s => renderHotspot(s.id, s.xPct, s.yPct, "fw"))}
          {VP_HOTSPOTS.map(s => renderHotspot(s.id, s.xPct, s.yPct, "vp", s.pill, s.rotate, s.opaque, s.transparent, s.size, s.noLabel, s.width, s.height, s.borderWidth))}
        </div>
      )}

      <div className={`live-input-strip ${liveInput ? "visible" : ""}`}>
        {liveInput && (
          <>
            <span className="live-input-physical">
              {liveInput.hotspotId ?? liveInput.physicalCode ?? "?"}
              {liveInput.physicalCode && (
                <span className="live-input-code"> ({liveInput.physicalCode})</span>
              )}
            </span>
            <span className="live-input-arrow">→</span>
            <span className="live-input-mapped">
              {liveInput.mappedLabel}
              <span className="live-input-code"> ({liveInput.virtualCode})</span>
            </span>
          </>
        )}
      </div>

      <div className="event-log-panel">
        <div className="event-log-header">
          <span className="event-log-title">Input Events</span>
          <button className="event-log-retry" onClick={onRetryMonitor} title="Retry monitor connection">
            ↺
          </button>
        </div>
        {eventLog && eventLog.length > 0 ? (
          <div className="event-log-entries">
            {eventLog.map((entry, i) => (
              <div key={i} className={`event-log-entry ${i === 0 ? "latest" : ""}`}>
                {entry}
              </div>
            ))}
          </div>
        ) : (
          <div className="event-log-empty">
            No events — press a button on the controller
          </div>
        )}
      </div>
    </div>
  );
}
