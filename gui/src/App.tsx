import { useState, useEffect, useCallback, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import Sidebar from "./components/Sidebar";
import ControllerView from "./components/ControllerView";
import AssignPanel from "./components/AssignPanel";
import ThumbsticksView from "./components/ThumbsticksView";
import VibrationView from "./components/VibrationView";
import {
  ProfileData, PresetProfileData, RemapMode, VirtualPadConfig, VibrationData,
  VP_SOURCE_CODES, VP_CODE_DISPLAY,
} from "./types";

const VP_CODE_TO_HOTSPOT: Record<string, string> = Object.fromEntries(
  Object.entries(VP_SOURCE_CODES).map(([id, code]) => [code, id])
);

export interface LiveInput {
  virtualCode: string;
  mappedLabel: string;
  physicalCode: string | null;
  hotspotId: string | null;
}

type ToastType = "success" | "error";

interface Toast {
  id: number;
  type: ToastType;
  msg: string;
}

let _nextId = 0;

export default function App() {
  // ── Firmware state ──────────────────────────────────────────
  const [profiles, setProfiles]           = useState<ProfileData[]>([]);
  const [activeProfile, setActiveProfile] = useState(1);
  const [loading, setLoading]             = useState(true);

  // ── Virtual pad state ───────────────────────────────────────
  const [vpConfig, setVpConfig]     = useState<VirtualPadConfig | null>(null);
  const [vpUpdating, setVpUpdating] = useState(false);
  const vpSaveTimer                 = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── View / thumbstick / trigger state ──────────────────────
  const [activeView, setActiveView]                   = useState<"controller" | "thumbsticks" | "triggers" | "vibration">("controller");
  const [presets, setPresets]                         = useState<PresetProfileData[]>([]);
  const [selectedPresetName, setSelectedPreset]       = useState<string | null>(null);
  const [presetsLoading, setPresetsLoading]           = useState(false);
  const [triggerPresets, setTriggerPresets]           = useState<PresetProfileData[]>([]);
  const [selectedTriggerPreset, setSelectedTrigger]   = useState<string | null>(null);
  const [triggerPresetsLoading, setTriggerPresetsLoading] = useState(false);

  // ── Vibration state ─────────────────────────────────────────
  const [vibrationData, setVibrationData] = useState<VibrationData[]>([]);

  // ── Shared UI state ─────────────────────────────────────────
  const [mode, setMode]                     = useState<RemapMode>("both");
  const [selectedButton, setSelectedButton] = useState<string | null>(null);
  const [deviceConnected, setDeviceConnected] = useState(true);
  const prevConnected                       = useRef(true);
  const [remapping, setRemapping]           = useState(false);
  const [flashMap, setFlashMap]             = useState<Record<string, number>>({});
  const [liveInput, setLiveInput]           = useState<LiveInput | null>(null);
  const liveInputTimer                      = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [eventLog, setEventLog]             = useState<string[]>([]);

  // ── Toast state ──────────────────────────────────────────────
  const [toasts, setToasts] = useState<Toast[]>([]);

  function pushToast(type: ToastType, msg: string) {
    const id = ++_nextId;
    const duration = type === "success" ? 3000 : 6000;
    setToasts(prev => [...prev, { id, type, msg }]);
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), duration);
  }

  function dismissToast(id: number) {
    setToasts(prev => prev.filter(t => t.id !== id));
  }

  // ── Load profiles ───────────────────────────────────────────
  const loadProfiles = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoading(true);
      const data = await invoke<ProfileData[]>("get_profiles");
      setProfiles(data);
    } catch (e) {
      if (!silent) pushToast("error", String(e));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // ── Load stick presets ──────────────────────────────────────
  const loadPresets = useCallback(async () => {
    setPresetsLoading(true);
    try {
      const data = await invoke<PresetProfileData[]>("get_presets");
      setPresets(data);
      setSelectedPreset(prev => {
        if (prev) return prev;
        const profileData = data.find(p => p.profile_number === 1);
        return profileData?.presets[0]?.name ?? null;
      });
    } catch (e) {
      pushToast("error", `Presets: ${e}`);
    } finally {
      setPresetsLoading(false);
    }
  }, []);

  // ── Load vibration data ─────────────────────────────────────
  const loadVibration = useCallback(async () => {
    try {
      const data = await invoke<VibrationData[]>("get_vibration");
      setVibrationData(data);
    } catch (e) {
      pushToast("error", `Vibration: ${e}`);
    }
  }, []);

  // ── Load trigger presets ────────────────────────────────────
  const loadTriggerPresets = useCallback(async () => {
    setTriggerPresetsLoading(true);
    try {
      const data = await invoke<PresetProfileData[]>("get_trigger_presets");
      setTriggerPresets(data);
      setSelectedTrigger(prev => {
        if (prev) return prev;
        const profileData = data.find(p => p.profile_number === 1);
        return profileData?.presets[0]?.name ?? null;
      });
    } catch (e) {
      pushToast("error", `Trigger presets: ${e}`);
    } finally {
      setTriggerPresetsLoading(false);
    }
  }, []);

  // ── Load vpad config ────────────────────────────────────────
  const loadVpConfig = useCallback(async () => {
    try {
      const cfg = await invoke<VirtualPadConfig>("get_vpad_config");
      setVpConfig(cfg);
    } catch (e) {
      pushToast("error", `Failed to load VP config: ${e}`);
    }
  }, []);

  useEffect(() => {
    loadProfiles();
    loadVpConfig();
    loadPresets();
    loadTriggerPresets();
    loadVibration();
    invoke("start_input_monitor").catch(() => {});
    const retryTimer = setTimeout(loadProfiles, 2000);
    return () => clearTimeout(retryTimer);
  }, [loadProfiles, loadVpConfig, loadPresets, loadTriggerPresets, loadVibration]);

  // Reload presets on profile change when in a preset view
  useEffect(() => {
    if (activeView === "thumbsticks") loadPresets();
    if (activeView === "triggers")    loadTriggerPresets();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProfile]);

  function handleViewChange(v: "controller" | "thumbsticks" | "triggers" | "vibration") {
    setActiveView(v);
    if (v === "thumbsticks") {
      const profileData = presets.find(p => p.profile_number === activeProfile);
      if (!selectedPresetName && profileData?.presets.length) {
        setSelectedPreset(profileData.presets[0].name);
      }
    }
    if (v === "triggers") {
      const profileData = triggerPresets.find(p => p.profile_number === activeProfile);
      if (!selectedTriggerPreset && profileData?.presets.length) {
        setSelectedTrigger(profileData.presets[0].name);
      }
    }
  }

  // ── Device polling ──────────────────────────────────────────
  useEffect(() => {
    async function poll() {
      const connected = await invoke<boolean>("check_device");
      setDeviceConnected(connected);
      if (connected && !prevConnected.current) {
        loadProfiles();
        pushToast("success", "Controller connected");
      }
      prevConnected.current = connected;
    }
    poll();
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Periodic profile refresh ────────────────────────────────
  useEffect(() => {
    const id = setInterval(() => {
      if (deviceConnected && !remapping) loadProfiles(true);
    }, 8000);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceConnected, remapping]);

  // ── Input monitor ───────────────────────────────────────────
  useEffect(() => {
    const unlisten = listen<{ code: string; source: string }>("input-press", (event) => {
      const { code, source } = event.payload;

      let hotspotId: string | null;
      let physicalCode: string | null;
      let virtualCode: string;
      let mappedLabel: string;

      if (source === "physical") {
        physicalCode = code;
        hotspotId    = VP_CODE_TO_HOTSPOT[code] ?? null;
        virtualCode  = vpConfig?.button_remap[code] ?? code;
        mappedLabel  = VP_CODE_DISPLAY[virtualCode] ?? virtualCode;
      } else {
        virtualCode  = code;
        mappedLabel  = VP_CODE_DISPLAY[code] ?? code;
        physicalCode = vpConfig
          ? (Object.entries(vpConfig.button_remap).find(([, v]) => v === code)?.[0] ?? null)
          : null;
        hotspotId    = physicalCode ? (VP_CODE_TO_HOTSPOT[physicalCode] ?? null) : null;
      }

      if (hotspotId) triggerFlash(hotspotId);

      const logEntry = `${hotspotId ?? physicalCode ?? code} → ${mappedLabel} [${source}]`;
      setEventLog(prev => [logEntry, ...prev].slice(0, 6));

      setLiveInput({ virtualCode, mappedLabel, physicalCode, hotspotId });
      if (liveInputTimer.current) clearTimeout(liveInputTimer.current);
      liveInputTimer.current = setTimeout(() => setLiveInput(null), 2500);
    });
    return () => { unlisten.then(fn => fn()); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vpConfig]);

  // ── Firmware remap ──────────────────────────────────────────
  async function handleFirmwareRemap(target: string) {
    if (!selectedButton) return;
    try {
      setRemapping(true);
      await invoke("remap_button", { source: selectedButton, target, profile: activeProfile });
      setSelectedButton(null);
      pushToast("success", `${selectedButton} → ${target} saved to firmware`);
      await new Promise(r => setTimeout(r, 400));
      await loadProfiles();
    } catch (e) {
      pushToast("error", String(e));
    } finally {
      setRemapping(false);
    }
  }

  // ── Firmware unmap ──────────────────────────────────────────
  async function handleFirmwareUnmap() {
    if (!selectedButton) return;
    try {
      setRemapping(true);
      await invoke("unmap_button", { source: selectedButton, profile: activeProfile });
      setSelectedButton(null);
      pushToast("success", `${selectedButton} mapping cleared`);
      await new Promise(r => setTimeout(r, 400));
      await loadProfiles();
    } catch (e) {
      pushToast("error", String(e));
    } finally {
      setRemapping(false);
    }
  }

  // ── VP remap ────────────────────────────────────────────────
  async function handleVPRemap(buttonId: string, targetCode: string) {
    if (!vpConfig) return;
    const srcCode = VP_SOURCE_CODES[buttonId];
    if (!srcCode) return;

    const updated: VirtualPadConfig = {
      ...vpConfig,
      button_remap: { ...vpConfig.button_remap, [srcCode]: targetCode },
    };
    setVpConfig(updated);
    setSelectedButton(null);

    try {
      await persistVpConfig(updated);
      const targetLabel = VP_CODE_DISPLAY[targetCode] ?? targetCode;
      pushToast("success", `${buttonId} → ${targetLabel} saved`);
    } catch (e) {
      pushToast("error", String(e));
    }
  }

  async function persistVpConfig(cfg: VirtualPadConfig) {
    if (vpSaveTimer.current) clearTimeout(vpSaveTimer.current);
    setVpUpdating(true);
    try {
      await invoke("set_vpad_config", { config: cfg });
    } catch (e) {
      pushToast("error", `VP config save failed: ${e}`);
    }
    vpSaveTimer.current = setTimeout(() => setVpUpdating(false), 1500);
  }

  function handleDeadzoneChange(field: string, value: number) {
    if (!vpConfig) return;
    const updated: VirtualPadConfig = {
      ...vpConfig,
      deadzones: { ...vpConfig.deadzones, [field]: value },
    };
    setVpConfig(updated);
    if (vpSaveTimer.current) clearTimeout(vpSaveTimer.current);
    vpSaveTimer.current = setTimeout(() => persistVpConfig(updated), 600);
  }

  // ── Hotspot helpers ─────────────────────────────────────────
  const FW_IDS = new Set(["P1", "P2", "P3", "P4", "S1", "S2"]);

  function triggerFlash(id: string) {
    setFlashMap(prev => ({ ...prev, [id]: (prev[id] ?? 0) + 1 }));
  }

  function handleHotspotClick(id: string) {
    const isFw = FW_IDS.has(id);
    if (mode !== "both") {
      if (isFw) setMode("firmware");
      else      setMode("vpad");
    }
    setSelectedButton(prev => prev === id ? null : id);
    triggerFlash(id);
  }

  // ── Derived ─────────────────────────────────────────────────
  const currentProfile = profiles.find(p => p.profile_number === activeProfile);

  const editMode: "firmware" | "vpad" = mode === "both"
    ? (selectedButton && FW_IDS.has(selectedButton) ? "firmware" : "vpad")
    : mode;

  const currentFwMapping = selectedButton && editMode === "firmware"
    ? currentProfile?.mappings.find(m => m.source_short === selectedButton)
    : undefined;

  const currentVpTarget = selectedButton && editMode === "vpad" && vpConfig
    ? VP_CODE_DISPLAY[vpConfig.button_remap[VP_SOURCE_CODES[selectedButton]] ?? ""] ?? null
    : null;

  const profilePresetItems = activeView === "triggers"
    ? (triggerPresets.find(p => p.profile_number === activeProfile)?.presets ?? [])
    : (presets.find(p => p.profile_number === activeProfile)?.presets ?? []);

  const currentSelectedPreset = activeView === "triggers" ? selectedTriggerPreset : selectedPresetName;
  const handlePresetSelect    = activeView === "triggers" ? setSelectedTrigger : setSelectedPreset;

  return (
    <div className="app">
      {!deviceConnected && (
        <div className="no-device-banner">
          No controller detected — plug in your SCUF Envision Pro
        </div>
      )}
      {vpUpdating && (
        <div className="vp-updating-banner">
          <span className="spinner" /> Updating virtual pad…
        </div>
      )}

      <div className="app-body">
        <Sidebar
          profiles={profiles}
          activeProfile={activeProfile}
          onProfileChange={n => { setActiveProfile(n); setSelectedButton(null); }}
          currentMappings={currentProfile?.mappings ?? []}
          selectedButton={selectedButton}
          onSelectButton={id => handleHotspotClick(id ?? "")}
          vpConfig={vpConfig}
          onDeadzoneChange={handleDeadzoneChange}
          onToast={pushToast}
          activeView={activeView}
          onViewChange={handleViewChange}
          presetItems={profilePresetItems}
          selectedPresetName={currentSelectedPreset}
          onPresetSelect={handlePresetSelect}
        />

        {activeView === "controller" ? (
          <>
            <ControllerView
              mode={mode}
              onModeChange={m => { setMode(m); setSelectedButton(null); }}
              selectedButton={selectedButton}
              onSelectButton={handleHotspotClick}
              mappings={currentProfile?.mappings ?? []}
              vpConfig={vpConfig}
              loading={loading}
              flashMap={flashMap}
              liveInput={liveInput}
              eventLog={eventLog}
              onRetryMonitor={() => invoke("start_input_monitor").catch(() => {})}
            />
            <AssignPanel
              visible={selectedButton !== null}
              mode={editMode}
              selectedButton={selectedButton}
              currentFwMapping={currentFwMapping}
              currentVpTarget={currentVpTarget}
              remapping={remapping}
              onFirmwareAssign={handleFirmwareRemap}
              onFirmwareUnmap={handleFirmwareUnmap}
              onVPAssign={handleVPRemap}
              onClose={() => setSelectedButton(null)}
            />
          </>
        ) : activeView === "thumbsticks" ? (
          <ThumbsticksView
            activeProfile={activeProfile}
            selectedPresetName={selectedPresetName}
            presets={presets}
            onPresetsReload={loadPresets}
            onToast={pushToast}
          />
        ) : activeView === "triggers" ? (
          <ThumbsticksView
            activeProfile={activeProfile}
            selectedPresetName={selectedTriggerPreset}
            presets={triggerPresets}
            onPresetsReload={loadTriggerPresets}
            onToast={pushToast}
            leftLabel="Left Trigger"
            rightLabel="Right Trigger"
            saveCommand="set_trigger_preset"
          />
        ) : (
          <VibrationView
            activeProfile={activeProfile}
            vibrationData={vibrationData}
            onDataReload={loadVibration}
            onToast={pushToast}
          />
        )}
      </div>

      {((presetsLoading && activeView === "thumbsticks") ||
        (triggerPresetsLoading && activeView === "triggers")) && (
        <div className="vp-updating-banner">
          <span className="spinner" /> Loading presets…
        </div>
      )}

      <div className="toast-stack">
        {toasts.map(t => (
          <div
            key={t.id}
            className={`toast toast--${t.type}`}
            onClick={() => dismissToast(t.id)}
          >
            <span className="toast-icon">{t.type === "success" ? "✓" : "⚠"}</span>
            <span className="toast-msg">{t.msg}</span>
          </div>
        ))}
        {toasts.length > 1 && (
          <button className="toast-clear-all" onClick={() => setToasts([])}>
            Clear all ({toasts.length})
          </button>
        )}
      </div>
    </div>
  );
}
