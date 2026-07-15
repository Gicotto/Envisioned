// ── Firmware remap types ─────────────────────────────────────

export type SourceKey = "P1" | "P2" | "P3" | "P4" | "S1" | "S2";

export interface Mapping {
  source: string;
  source_short: string;
  target_keyname: string;
  target_short: string;
}

export interface ProfileData {
  profile_number: number;
  slot: string;
  mappings: Mapping[];
}

export interface TargetGroup {
  label: string;
  targets: { short: string; display: string }[];
}

export const TARGET_GROUPS: TargetGroup[] = [
  {
    label: "Face",
    targets: [
      { short: "A", display: "A" },
      { short: "B", display: "B" },
      { short: "X", display: "X" },
      { short: "Y", display: "Y" },
    ],
  },
  {
    label: "D-Pad",
    targets: [
      { short: "UP",    display: "↑" },
      { short: "DOWN",  display: "↓" },
      { short: "LEFT",  display: "←" },
      { short: "RIGHT", display: "→" },
    ],
  },
  {
    label: "Bumpers",
    targets: [
      { short: "LB", display: "LB" },
      { short: "RB", display: "RB" },
    ],
  },
  {
    label: "Triggers",
    targets: [
      { short: "LT", display: "LT" },
      { short: "RT", display: "RT" },
    ],
  },
  {
    label: "Sticks",
    targets: [
      { short: "L3", display: "L3" },
      { short: "R3", display: "R3" },
    ],
  },
];

export const SOURCE_LABEL: Record<string, string> = {
  GamepadP1: "P1", GamepadP2: "P2",
  GamepadP3: "P3", GamepadP4: "P4",
  GamepadS1: "S1", GamepadS2: "S2",
};

export const PROFILE_COLORS = ["#d4006b", "#b800d4", "#6b00d4"];

// ── Thumbstick preset types ──────────────────────────────────

export type CurveType = "dynamic" | "linear" | "exponential" | "aggressive" | "custom";

export const CURVE_TYPES: { value: CurveType; label: string }[] = [
  { value: "dynamic",     label: "Dynamic"     },
  { value: "linear",      label: "Linear"      },
  { value: "exponential", label: "Exponential" },
  { value: "aggressive",  label: "Aggressive"  },
  { value: "custom",      label: "Custom"      },
];

export interface StickPreset {
  side: "left" | "right";
  curve: number;
  curve_name: CurveType;
  deadzone: number;
  max_deadzone: number;
}

export interface ThumbstickPreset {
  name: string;
  predefined: boolean;
  sticks: StickPreset[];
}

export interface PresetProfileData {
  profile_number: number;
  slot: string;
  presets: ThumbstickPreset[];
}

// ── Vibration types ──────────────────────────────────────────

export interface VibrationData {
  profile_number: number;
  left: number;
  right: number;
}

// ── Virtual pad types ────────────────────────────────────────

export type RemapMode = "firmware" | "vpad" | "both";

export interface VirtualPadConfig {
  button_remap: Record<string, string>;
  deadzones: {
    left_deadzone: number;
    left_jitter: number;
    right_deadzone: number;
    right_jitter: number;
  };
}

// Raw BTN_ code the SCUF hardware sends for each labelled button
export const VP_SOURCE_CODES: Record<string, string> = {
  A:      "BTN_SOUTH",
  B:      "BTN_EAST",
  X:      "BTN_C",      // SCUF reports X as BTN_C
  Y:      "BTN_NORTH",
  LB:     "BTN_WEST",   // SCUF reports LB as BTN_WEST
  RB:     "BTN_Z",      // SCUF reports RB as BTN_Z
  L3:     "BTN_TL2",
  R3:     "BTN_TR2",
  Start:  "BTN_TR",     // SCUF reports Start as BTN_TR
  Select: "BTN_TL",     // SCUF reports Select as BTN_TL
};

export interface VPTargetGroup {
  label: string;
  targets: { label: string; code: string }[];
}

export const VP_TARGET_GROUPS: VPTargetGroup[] = [
  {
    label: "Face",
    targets: [
      { label: "A",  code: "BTN_SOUTH" },
      { label: "B",  code: "BTN_EAST"  },
      { label: "X",  code: "BTN_WEST"  },
      { label: "Y",  code: "BTN_NORTH" },
    ],
  },
  {
    label: "Bumpers",
    targets: [
      { label: "LB", code: "BTN_TL" },
      { label: "RB", code: "BTN_TR" },
    ],
  },
  {
    label: "Sticks",
    targets: [
      { label: "L3", code: "BTN_THUMBL" },
      { label: "R3", code: "BTN_THUMBR" },
    ],
  },
  {
    label: "Menu",
    targets: [
      { label: "Start",  code: "BTN_START"  },
      { label: "Select", code: "BTN_SELECT" },
      { label: "Guide",  code: "BTN_MODE"   },
    ],
  },
];

// code → display label (for showing current VP assignment)
export const VP_CODE_DISPLAY: Record<string, string> = Object.fromEntries(
  VP_TARGET_GROUPS.flatMap(g => g.targets.map(t => [t.code, t.label]))
);

export interface VPHotspot {
  id: string;
  xPct: number;
  yPct: number;
  pill?: boolean;
  rotate?: number;
  opaque?: boolean;
  transparent?: boolean;
  size?: number;    // px, sets both width and height
  width?: number;   // px, overrides width independently
  height?: number;  // px, overrides height independently
  noLabel?: boolean; // no pink label
  borderWidth?: number; // px, overrides default 2px border
}

// VP hotspots — all at left edge; user adjusts xPct/yPct in ControllerView.tsx
// default size is 24px
export const VP_HOTSPOTS: VPHotspot[] = [
  { id: "A",      xPct: 47,   yPct: 38, transparent: true, noLabel: true   },
  { id: "B",      xPct: 50.30,   yPct: 30, transparent: true, noLabel: true   },
  { id: "X",      xPct: 43.85,   yPct: 30, transparent: true, noLabel: true   },
  { id: "Y",      xPct: 47,   yPct: 22.5, transparent: true, noLabel: true   },
  { id: "RB",     xPct: 63.5,   yPct: 9, transparent: true, size: 20, noLabel: true, borderWidth: 1.5    },
  { id: "LB",     xPct: 82.5,   yPct: 9, transparent: true, size: 20, noLabel: true, borderWidth: 1.5    },
  { id: "RT",     xPct: 59, yPct: 17.75, transparent: true, size: 20, noLabel: true, borderWidth: 1.5 },
  { id: "LT",     xPct: 86.95, yPct: 17.75, transparent: true, size: 20, noLabel: true, borderWidth: 1.5 },
  { id: "L3",     xPct: 25,   yPct: 45.5, transparent: true, size: 28, noLabel: true, borderWidth: 3   },
  { id: "R3",     xPct: 40.5, yPct: 45.5, transparent: true, size: 28, noLabel: true, borderWidth: 3 },
  { id: "Start",  xPct: 41.90,   yPct: 21.25, transparent: true,  pill: true, rotate: -70, width: 22, height: 12, noLabel: true },
  { id: "Select", xPct: 23.5, yPct: 21.25, transparent: true,  pill: true, rotate: 70, width: 22, height: 12, noLabel: true },
];
