import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Mapping, ProfileData, PROFILE_COLORS, SOURCE_LABEL, ThumbstickPreset, VirtualPadConfig } from "../types";

interface Props {
  profiles: ProfileData[];
  activeProfile: number;
  onProfileChange: (n: number) => void;
  currentMappings: Mapping[];
  selectedButton: string | null;
  onSelectButton: (src: string | null) => void;
  vpConfig: VirtualPadConfig | null;
  onDeadzoneChange: (field: string, value: number) => void;
  onToast: (type: "success" | "error", msg: string) => void;
  // view switching
  activeView: "controller" | "thumbsticks" | "triggers" | "vibration";
  onViewChange: (v: "controller" | "thumbsticks" | "triggers" | "vibration") => void;
  // thumbstick preset list
  presetItems: ThumbstickPreset[];
  selectedPresetName: string | null;
  onPresetSelect: (name: string) => void;
}

export default function Sidebar({
  profiles, activeProfile, onProfileChange,
  currentMappings, selectedButton, onSelectButton,
  onToast,
  activeView, onViewChange,
  presetItems, selectedPresetName, onPresetSelect,
}: Props) {
  const profileCount = Math.max(profiles.length, 3);
  const [bridgeOn,        setBridgeOn]        = useState(false);
  const [bridgeBusy,      setBridgeBusy]      = useState(false);
  const [svcInstalled,    setSvcInstalled]    = useState<boolean | null>(null);
  const [hidrawOk,        setHidrawOk]        = useState<boolean | null>(null);
  const [udevBusy,        setUdevBusy]        = useState(false);
  const [udevManualCmd,   setUdevManualCmd]   = useState<string | null>(null);
  const [installing,      setInstalling]      = useState(false);
  const [installError,    setInstallError]    = useState<string | null>(null);
  const [brightness,      setBrightness]      = useState(66);
  const brightnessTimer                       = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [ecoOn,           setEcoOn]           = useState(false);
  const [shutoffOn,       setShutoffOn]       = useState(false);
  const [shutoffMinutes,  setShutoffMinutes]  = useState(10);

  useEffect(() => {
    async function poll() {
      const installed = await invoke<boolean>("service_installed");
      setSvcInstalled(installed);
      if (installed) {
        const running = await invoke<boolean>("bridge_running");
        setBridgeOn(running);
        const ok = await invoke<boolean>("hidraw_accessible");
        setHidrawOk(ok);
      }
    }
    poll();
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, []);

  async function handleInstall() {
    setInstalling(true);
    setInstallError(null);
    try {
      await invoke("install_service");
      setSvcInstalled(true);
      onToast("success", "Virtual pad service installed — starts automatically at login");
    } catch (e) {
      const msg = String(e);
      setInstallError(msg);
      onToast("error", `Install failed: ${msg}`);
    } finally {
      setInstalling(false);
    }
  }

  async function handleUdevInstall() {
    setUdevBusy(true);
    setUdevManualCmd(null);
    try {
      await invoke("install_udev");
      setHidrawOk(true);
      onToast("success", "Permissions fixed — virtual pad ready");
    } catch (e) {
      const msg = String(e);
      if (msg.startsWith("MANUAL_CMD:")) {
        setUdevManualCmd(msg.slice("MANUAL_CMD:".length));
      } else {
        onToast("error", `Permission fix failed: ${msg}`);
      }
    } finally {
      setUdevBusy(false);
    }
  }

  async function toggleBridge() {
    setBridgeBusy(true);
    try {
      if (bridgeOn) {
        await invoke("stop_bridge");
        setBridgeOn(false);
        onToast("success", "Virtual pad stopped — real controller restored");
      } else {
        await invoke("start_bridge");
        setBridgeOn(true);
        onToast("success", "Virtual pad started — real controller hidden");
        (async () => {
          for (let i = 0; i < 10; i++) {
            try {
              const result = await invoke<string>("start_input_monitor");
              if (result === "already-virtual" || result.startsWith("started-virtual")) return;
            } catch { /* not ready */ }
            await new Promise(r => setTimeout(r, 400));
          }
        })();
      }
    } catch (e) {
      onToast("error", String(e));
    } finally {
      setBridgeBusy(false);
    }
  }

  function handleEcoToggle() {
    const next = !ecoOn;
    setEcoOn(next);
    invoke("set_eco_mode", { enabled: next }).catch((e: unknown) => onToast("error", String(e)));
  }

  function handleShutoffToggle() {
    const next = !shutoffOn;
    setShutoffOn(next);
    invoke("set_auto_shutoff", { enabled: next, minutes: next ? shutoffMinutes : undefined })
      .catch((e: unknown) => onToast("error", String(e)));
  }

  function handleShutoffMinutes(min: number) {
    setShutoffMinutes(min);
    if (shutoffOn) {
      invoke("set_auto_shutoff", { enabled: true, minutes: min })
        .catch((e: unknown) => onToast("error", String(e)));
    }
  }

  function handleBrightnessChange(val: number) {
    setBrightness(val);
    if (brightnessTimer.current) clearTimeout(brightnessTimer.current);
    brightnessTimer.current = setTimeout(() => {
      invoke("set_brightness", { level: val }).catch((e: unknown) => onToast("error", String(e)));
    }, 300);
  }

  return (
    <aside className="sidebar">
      {/* Header */}
      <div className="sidebar-header">
        <div className="sidebar-device-name">SCUF Envision Pro</div>
        <div className="sidebar-profiles">
          {Array.from({ length: profileCount }, (_, i) => {
            const n = i + 1;
            return (
              <button
                key={n}
                className={`profile-chip ${activeProfile === n ? "active" : ""}`}
                style={{ background: PROFILE_COLORS[i] ?? "#555" }}
                onClick={() => onProfileChange(n)}
                title={`Profile ${n}`}
              >
                {n}
              </button>
            );
          })}
        </div>
      </div>

      {/* Bridge / service row */}
      {svcInstalled === false ? (
        <div className="bridge-row bridge-row--install">
          <span className="bridge-label bridge-label--warn">
            Virtual pad not installed
          </span>
          <button
            className="bridge-install-btn"
            onClick={handleInstall}
            disabled={installing}
            title="Install systemd user service so virtual pad runs in background"
          >
            {installing ? "Installing…" : "Install"}
          </button>
          {installError && (
            <div className="bridge-install-error" title={installError}>!</div>
          )}
        </div>
      ) : (
        <>
          {hidrawOk === false && (
            <div className="bridge-perm-block">
              <div className="bridge-row bridge-row--install">
                <div className="bridge-perm-info">
                  <span className="bridge-label bridge-label--warn">
                    Controller not accessible
                  </span>
                  <span className="bridge-label-sub">
                    Use "Re-apply device permissions" below
                  </span>
                </div>
              </div>
            </div>
          )}
          <div className="bridge-row">
            <span className={`bridge-dot ${bridgeOn ? "on" : ""}`} />
            <span className="bridge-label">
              {bridgeOn ? "Virtual Pad Active" : "Virtual Pad Off"}
            </span>
            <button
              className={`bridge-toggle ${bridgeOn ? "on" : ""}`}
              onClick={toggleBridge}
              disabled={bridgeBusy || svcInstalled === null || hidrawOk === false}
            >
              {bridgeOn ? "Stop" : "Start"}
            </button>
          </div>
          <div className="bridge-row bridge-row--reapply">
            <button
              className="bridge-reapply-btn"
              onClick={handleUdevInstall}
              disabled={udevBusy}
              title="Re-write udev rules (run after updates or if Steam sees duplicate controller)"
            >
              {udevBusy ? "Applying…" : "Re-apply device permissions"}
            </button>
            {udevManualCmd && (
              <div className="bridge-manual-cmd">
                <span className="bridge-label-sub">Auth failed — run in terminal:</span>
                <div className="bridge-manual-cmd-row">
                  <code className="bridge-manual-cmd-text">{udevManualCmd}</code>
                  <button
                    className="bridge-copy-btn"
                    onClick={() => navigator.clipboard.writeText(udevManualCmd)}
                  >Copy</button>
                </div>
              </div>
            )}
          </div>
        </>
      )}

      {/* Brightness */}
      <div className="brightness-row">
        <span className="brightness-label">Brightness</span>
        <input
          type="range"
          className="brightness-slider"
          min={0} max={100} value={brightness}
          onChange={e => handleBrightnessChange(Number(e.target.value))}
        />
        <span className="brightness-pct">{brightness}%</span>
      </div>

      {/* Eco mode */}
      <div className="global-row">
        <span className="global-row-label">Eco Mode</span>
        <button
          className={`toggle-pill ${ecoOn ? "on" : ""}`}
          onClick={handleEcoToggle}
        >
          {ecoOn ? "On" : "Off"}
        </button>
      </div>

      {/* Auto shutoff */}
      <div className="global-row global-row--shutoff">
        <span className="global-row-label">Auto Shutoff</span>
        <button
          className={`toggle-pill ${shutoffOn ? "on" : ""}`}
          onClick={handleShutoffToggle}
        >
          {shutoffOn ? "On" : "Off"}
        </button>
        {shutoffOn && (
          <select
            className="shutoff-select"
            value={shutoffMinutes}
            onChange={e => handleShutoffMinutes(Number(e.target.value))}
          >
            {[5, 10, 15, 20, 30, 60].map(m => (
              <option key={m} value={m}>{m} min</option>
            ))}
          </select>
        )}
      </div>

      {/* Tab nav */}
      <div className="sidebar-nav">
        <button
          className={`sidebar-nav-tab ${activeView === "controller" ? "active" : ""}`}
          onClick={() => onViewChange("controller")}
        >
          Mappings
        </button>
        <button
          className={`sidebar-nav-tab ${activeView === "thumbsticks" ? "active" : ""}`}
          onClick={() => onViewChange("thumbsticks")}
        >
          Sticks
        </button>
        <button
          className={`sidebar-nav-tab ${activeView === "triggers" ? "active" : ""}`}
          onClick={() => onViewChange("triggers")}
        >
          Triggers
        </button>
        <button
          className={`sidebar-nav-tab ${activeView === "vibration" ? "active" : ""}`}
          onClick={() => onViewChange("vibration")}
        >
          Vibration
        </button>
      </div>

      {/* Tab content */}
      {activeView === "vibration" ? null : activeView === "controller" ? (
        <div className="sidebar-mappings">
          <div className="sidebar-mappings-header">
            <span className="sidebar-mappings-title">Custom Mappings</span>
          </div>
          {(["P1", "P2", "P3", "P4", "S1", "S2"] as const).map(id => {
            const m = currentMappings.find(x => (x.source_short || SOURCE_LABEL[x.source]) === id);
            const isSelected = selectedButton === id;
            return (
              <div
                key={id}
                className={`mapping-row ${isSelected ? "selected" : ""} ${!m ? "mapping-row--none" : ""}`}
                onClick={() => onSelectButton(isSelected ? null : id)}
              >
                <div className="mapping-row-icon">{id}</div>
                <div className="mapping-row-info">
                  <div className="mapping-row-target">{m ? m.target_short : "None"}</div>
                  <div className="mapping-row-source">{m ? m.target_keyname : "—"}</div>
                </div>
                <div className="mapping-badge">{id}</div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="preset-list">
          <div className="preset-list-header">
            <span className="preset-list-title">
              {activeView === "triggers" ? "Trigger Presets" : "Stick Presets"}
            </span>
          </div>
          {presetItems.length === 0 ? (
            <div className="sidebar-empty">No presets found</div>
          ) : (
            presetItems.map(preset => (
              <div
                key={preset.name}
                className={`preset-item ${selectedPresetName === preset.name ? "selected" : ""}`}
                onClick={() => onPresetSelect(preset.name)}
              >
                <span className="preset-item-name">{preset.name}</span>
                {preset.predefined && (
                  <span className="preset-item-badge">HW</span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </aside>
  );
}
