import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { CurveType, CURVE_TYPES, PresetProfileData, StickPreset } from "../types";

// Reference curves: [input%, output%] used for non-custom type visualization
const PRESET_CURVES: Record<Exclude<CurveType, "custom">, [number, number][]> = {
  linear:      [[0,0],[20,20],[40,40],[60,60],[80,80],[100,100]],
  dynamic:     [[0,0],[20,5], [40,15],[60,35],[80,65],[100,100]],
  exponential: [[0,0],[20,2], [40,8], [60,22],[80,52],[100,100]],
  aggressive:  [[0,0],[20,38],[40,65],[60,82],[80,93],[100,100]],
};

function makeSvgPath(pts: [number, number][], W: number, H: number): string {
  const sx = (v: number) => (v / 100) * W;
  const sy = (v: number) => H - (v / 100) * H;
  const m = pts.map(([x, y]) => [sx(x), sy(y)] as [number, number]);
  if (m.length < 2) return "";
  let d = `M ${m[0][0]},${m[0][1]}`;
  for (let i = 0; i < m.length - 1; i++) {
    const p0 = m[Math.max(0, i - 1)];
    const p1 = m[i];
    const p2 = m[i + 1];
    const p3 = m[Math.min(m.length - 1, i + 2)];
    const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
    const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
    const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
    const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2[0]},${p2[1]}`;
  }
  return d;
}

function CurveVisualizer({ stick }: { stick: StickPreset }) {
  const W = 160, H = 110;
  const pts: [number, number][] =
    stick.curve_name === "custom"
      ? [[0,0],[20,5],[40,15],[60,30],[80,60],[100,100]] // fallback until custom pts exposed
      : PRESET_CURVES[stick.curve_name as Exclude<CurveType, "custom">] ?? PRESET_CURVES.linear;

  const pathD = makeSvgPath(pts, W, H);
  const areaD = pathD + ` L ${W},${H} L 0,${H} Z`;

  return (
    <div className="curve-viz-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="curve-svg" preserveAspectRatio="none">
        {[25, 50, 75].map(v => (
          <g key={v}>
            <line x1={v / 100 * W} y1={0} x2={v / 100 * W} y2={H}
              stroke="rgba(255,255,255,0.05)" strokeWidth="0.5" />
            <line x1={0} y1={H - v / 100 * H} x2={W} y2={H - v / 100 * H}
              stroke="rgba(255,255,255,0.05)" strokeWidth="0.5" />
          </g>
        ))}
        <line x1={0} y1={H} x2={W} y2={0}
          stroke="rgba(255,255,255,0.08)" strokeWidth="1" strokeDasharray="3,3" />
        <path d={areaD} fill="rgba(196,0,90,0.10)" />
        <path d={pathD} fill="none" stroke="#c4005a" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div className="curve-axis-caption">Physical movement → Output value</div>
    </div>
  );
}

function DeadzoneControl({
  label, value, min, max, onChange,
}: {
  label: string; value: number; min: number; max: number;
  onChange: (v: number) => void;
}) {
  const clamp = (v: number) => Math.max(min, Math.min(max, Math.round(v)));
  return (
    <div className="hw-dz-row">
      <div className="hw-dz-header">
        <span className="hw-dz-label">{label}</span>
        <div className="hw-dz-input-group">
          <button className="hw-dz-step" onClick={() => onChange(clamp(value - 1))}>−</button>
          <input
            className="hw-dz-num"
            type="number"
            min={min} max={max} value={value}
            onChange={e => onChange(clamp(Number(e.target.value) || 0))}
          />
          <span className="hw-dz-pct">%</span>
          <button className="hw-dz-step" onClick={() => onChange(clamp(value + 1))}>+</button>
        </div>
      </div>
      <input
        type="range" className="dz-slider"
        min={min} max={max} value={value}
        onChange={e => onChange(Number(e.target.value))}
      />
    </div>
  );
}

function StickPanel({
  title, stick, onCurveChange, onDeadzoneChange,
}: {
  title: string;
  stick: StickPreset;
  onCurveChange: (c: CurveType) => void;
  onDeadzoneChange: (f: "deadzone" | "max_deadzone", v: number) => void;
}) {
  return (
    <div className="stick-panel-card">
      <div className="stick-panel-title">{title}</div>

      <div className="curve-type-row">
        {CURVE_TYPES.map(ct => (
          <button
            key={ct.value}
            className={`curve-type-btn ${stick.curve_name === ct.value ? "active" : ""}`}
            onClick={() => onCurveChange(ct.value)}
          >
            {ct.label}
          </button>
        ))}
      </div>

      <CurveVisualizer stick={stick} />

      <div className="hw-dz-section">
        <DeadzoneControl
          label="Min Deadzone"
          value={stick.deadzone}
          min={0} max={99}
          onChange={v => onDeadzoneChange("deadzone", v)}
        />
        <DeadzoneControl
          label="Max Deadzone"
          value={stick.max_deadzone}
          min={1} max={100}
          onChange={v => onDeadzoneChange("max_deadzone", v)}
        />
      </div>
    </div>
  );
}

type EditSticks = { left: StickPreset; right: StickPreset };

interface Props {
  activeProfile: number;
  selectedPresetName: string | null;
  presets: PresetProfileData[];
  onPresetsReload: () => Promise<void>;
  onToast: (type: "success" | "error", msg: string) => void;
  leftLabel?: string;
  rightLabel?: string;
  saveCommand?: string;
}

export default function ThumbsticksView({
  activeProfile, selectedPresetName, presets, onPresetsReload, onToast,
  leftLabel = "Left Thumbstick",
  rightLabel = "Right Thumbstick",
  saveCommand = "set_preset",
}: Props) {
  const profileData    = presets.find(p => p.profile_number === activeProfile);
  const selectedPreset = profileData?.presets.find(p => p.name === selectedPresetName);

  const [editSticks, setEditSticks] = useState<EditSticks | null>(null);
  const [saving, setSaving]         = useState(false);
  const saveTimer                   = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    if (selectedPreset) {
      const left  = selectedPreset.sticks.find(s => s.side === "left");
      const right = selectedPreset.sticks.find(s => s.side === "right");
      if (left && right) setEditSticks({ left: { ...left }, right: { ...right } });
      else setEditSticks(null);
    } else {
      setEditSticks(null);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPresetName]);

  async function doSave(sticks: EditSticks) {
    if (!selectedPresetName) return;
    setSaving(true);
    try {
      await invoke(saveCommand, {
        preset_name:  selectedPresetName,
        profile:      activeProfile,
        left_curve:   sticks.left.curve_name,
        right_curve:  sticks.right.curve_name,
        left_dz:      sticks.left.deadzone,
        left_max_dz:  sticks.left.max_deadzone,
        right_dz:     sticks.right.deadzone,
        right_max_dz: sticks.right.max_deadzone,
      });
      await onPresetsReload();
    } catch (e) {
      onToast("error", String(e));
    } finally {
      setSaving(false);
    }
  }

  function handleCurveChange(side: "left" | "right", curve: CurveType) {
    if (!editSticks) return;
    const updated = { ...editSticks, [side]: { ...editSticks[side], curve_name: curve } };
    setEditSticks(updated);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    doSave(updated);
  }

  function handleDeadzoneChange(side: "left" | "right", field: "deadzone" | "max_deadzone", value: number) {
    if (!editSticks) return;
    const updated = { ...editSticks, [side]: { ...editSticks[side], [field]: value } };
    setEditSticks(updated);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => doSave(updated), 500);
  }

  if (!selectedPresetName || !editSticks) {
    return (
      <div className="thumbsticks-view thumbsticks-view--empty">
        <span className="thumbsticks-empty-msg">Select a preset from the sidebar</span>
      </div>
    );
  }

  return (
    <div className="thumbsticks-view">
      <div className="thumbsticks-header">
        <span className="thumbsticks-preset-name">{selectedPresetName}</span>
        {selectedPreset?.predefined && (
          <span className="thumbsticks-tag">Built-in</span>
        )}
        {saving && <span className="thumbsticks-saving">Saving…</span>}
      </div>
      <div className="thumbsticks-panels">
        <StickPanel
          title={leftLabel}
          stick={editSticks.left}
          onCurveChange={c => handleCurveChange("left", c)}
          onDeadzoneChange={(f, v) => handleDeadzoneChange("left", f, v)}
        />
        <StickPanel
          title={rightLabel}
          stick={editSticks.right}
          onCurveChange={c => handleCurveChange("right", c)}
          onDeadzoneChange={(f, v) => handleDeadzoneChange("right", f, v)}
        />
      </div>
    </div>
  );
}
