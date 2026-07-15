import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { VibrationData } from "../types";

interface Props {
  activeProfile: number;
  vibrationData: VibrationData[];
  onDataReload: () => Promise<void>;
  onToast: (type: "success" | "error", msg: string) => void;
}

export default function VibrationView({
  activeProfile, vibrationData, onDataReload, onToast,
}: Props) {
  const current = vibrationData.find(v => v.profile_number === activeProfile);

  const [left,  setLeft]  = useState(current?.left  ?? 50);
  const [right, setRight] = useState(current?.right ?? 50);
  const [synced, setSynced] = useState(false);
  const [saving, setSaving] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Sync local state when profile changes or data reloads
  useEffect(() => {
    if (current) {
      setLeft(current.left);
      setRight(current.right);
    }
  }, [activeProfile, current?.left, current?.right]);

  async function doSave(l: number, r: number) {
    setSaving(true);
    try {
      await invoke("set_vibration", { profile: activeProfile, left: l, right: r });
      await onDataReload();
    } catch (e) {
      onToast("error", String(e));
    } finally {
      setSaving(false);
    }
  }

  function schedSave(l: number, r: number) {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => doSave(l, r), 400);
  }

  function handleLeft(val: number) {
    const clamped = Math.max(0, Math.min(100, val));
    setLeft(clamped);
    const r = synced ? clamped : right;
    if (synced) setRight(clamped);
    schedSave(clamped, r);
  }

  function handleRight(val: number) {
    const clamped = Math.max(0, Math.min(100, val));
    setRight(clamped);
    const l = synced ? clamped : left;
    if (synced) setLeft(clamped);
    schedSave(l, clamped);
  }

  function handleSyncToggle() {
    const next = !synced;
    setSynced(next);
    if (next) {
      // snap right to left when enabling sync
      setRight(left);
      schedSave(left, left);
    }
  }

  return (
    <div className="vibration-view">
      <div className="vibration-header">
        <span className="vibration-title">Vibration</span>
        <span className="vibration-profile">Profile {activeProfile}</span>
        {saving && <span className="vibration-saving">Saving…</span>}
        <button
          className={`vib-sync-btn ${synced ? "active" : ""}`}
          onClick={handleSyncToggle}
          title="Link left and right together"
        >
          {synced ? "Sync On" : "Sync Off"}
        </button>
      </div>

      <div className="vibration-panels">
        <VibPanel
          label="Left Module"
          value={left}
          onChange={handleLeft}
        />
        <div className={`vib-link-line ${synced ? "active" : ""}`} />
        <VibPanel
          label="Right Module"
          value={right}
          onChange={handleRight}
        />
      </div>
    </div>
  );
}

function VibPanel({ label, value, onChange }: {
  label: string;
  value: number;
  onChange: (v: number) => void;
}) {
  const clamp = (v: number) => Math.max(0, Math.min(100, Math.round(v)));
  return (
    <div className="vib-panel-card">
      <div className="vib-panel-title">{label}</div>

      <div className="vib-motor-icon">
        <svg viewBox="0 0 40 40" className="vib-motor-svg">
          <circle cx="20" cy="20" r="14" fill="none"
            stroke="rgba(255,255,255,0.06)" strokeWidth="3" />
          <circle cx="20" cy="20" r="14" fill="none"
            stroke="#c4005a"
            strokeWidth="3"
            strokeDasharray={`${value * 0.88} 88`}
            strokeDashoffset="22"
            strokeLinecap="round"
            style={{ transition: "stroke-dasharray 0.15s" }}
          />
          <text x="20" y="25" textAnchor="middle"
            fontSize="11" fontWeight="700" fill="var(--text)">
            {value}%
          </text>
        </svg>
      </div>

      <div className="vib-slider-row">
        <button className="hw-dz-step" onClick={() => onChange(clamp(value - 1))}>−</button>
        <input
          type="range"
          className="dz-slider"
          min={0} max={100} value={value}
          onChange={e => onChange(Number(e.target.value))}
        />
        <button className="hw-dz-step" onClick={() => onChange(clamp(value + 1))}>+</button>
      </div>

      <div className="vib-num-row">
        <input
          className="hw-dz-num"
          type="number"
          min={0} max={100}
          value={value}
          onChange={e => onChange(clamp(Number(e.target.value) || 0))}
        />
        <span className="hw-dz-pct">%</span>
      </div>
    </div>
  );
}
