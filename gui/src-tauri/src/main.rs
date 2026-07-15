// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Emitter, Manager};

const SERVICE_NAME: &str = "scuf-virtual-pad";
const GUI_LOG: &str = "/tmp/scuf-gui.log";

// ── Logging ───────────────────────────────────────────────────

fn log(level: &str, msg: &str) {
    let ts = chrono_now();
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(GUI_LOG)
    {
        let _ = writeln!(f, "{ts} {level:<8} {msg}");
    }
}

fn chrono_now() -> String {
    // No chrono dep — use SystemTime formatted as seconds since epoch
    // Format: seconds.millis (good enough for a log)
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    // Convert to a rough datetime string via seconds
    let secs = d.as_secs();
    let ms   = d.subsec_millis();
    // Simple ISO-ish: we can't do full date without chrono, so use unix ts
    format!("[{secs}.{ms:03}]")
}

macro_rules! linfo  { ($($a:tt)*) => { log("INFO",  &format!($($a)*)) } }
macro_rules! lerror { ($($a:tt)*) => { log("ERROR", &format!($($a)*)) } }
#[allow(unused_macros)]
macro_rules! lwarn  { ($($a:tt)*) => { log("WARN",  &format!($($a)*)) } }

// ── Types ─────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct VibrationData {
    pub profile_number: u8,
    pub left: u8,
    pub right: u8,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Mapping {
    pub source: String,
    pub source_short: String,
    pub target_keyname: String,
    pub target_short: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ProfileData {
    pub profile_number: u8,
    pub slot: String,
    pub mappings: Vec<Mapping>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct StickPreset {
    pub side: String,
    pub curve: u8,
    pub curve_name: String,
    pub deadzone: u8,
    pub max_deadzone: u8,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ThumbstickPreset {
    pub name: String,
    pub predefined: bool,
    pub sticks: Vec<StickPreset>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct PresetProfileData {
    pub profile_number: u8,
    pub slot: String,
    pub presets: Vec<ThumbstickPreset>,
}

// ── Path helpers ─────────────────────────────────────────────

fn scuf_script() -> String {
    std::env::var("SCUF_CONFIG_PATH")
        .unwrap_or_else(|_| "../scuf_config.py".to_string())
}

fn vpad_config_path() -> std::path::PathBuf {
    let p = std::env::var("SCUF_VPAD_CONFIG")
        .unwrap_or_else(|_| "../virtual_pad_config.json".to_string());
    std::path::PathBuf::from(p)
}

fn default_vpad_config() -> serde_json::Value {
    serde_json::json!({
        "button_remap": {
            "BTN_SOUTH": "BTN_SOUTH",
            "BTN_EAST":  "BTN_EAST",
            "BTN_NORTH": "BTN_NORTH",
            "BTN_C":     "BTN_WEST",
            "BTN_WEST":  "BTN_TL",
            "BTN_Z":     "BTN_TR",
            "BTN_TR":    "BTN_START",
            "BTN_TL":    "BTN_SELECT",
            "BTN_TL2":   "BTN_THUMBL",
            "BTN_TR2":   "BTN_THUMBR"
        },
        "deadzones": {
            "left_deadzone":  0,
            "left_jitter":    0,
            "right_deadzone": 0,
            "right_jitter":   0
        }
    })
}

/// Resolve the absolute path to scuf_virtual_pad_managed.py.
/// Priority: SCUF_PAD_PATH env → next to binary → cwd-relative (dev).
fn resolve_pad_script() -> Result<std::path::PathBuf, String> {
    if let Ok(p) = std::env::var("SCUF_PAD_PATH") {
        linfo!("Using SCUF_PAD_PATH={p}");
        return Ok(std::path::PathBuf::from(p));
    }
    if let Ok(exe) = std::env::current_exe() {
        let candidate = exe
            .parent()
            .unwrap_or(std::path::Path::new("."))
            .join("scuf_virtual_pad_managed.py");
        if candidate.exists() {
            let resolved = std::fs::canonicalize(&candidate).map_err(|e| e.to_string())?;
            linfo!("Resolved pad script (next to binary): {}", resolved.display());
            return Ok(resolved);
        }
    }
    // Dev fallback: cwd is src-tauri/, script is one level up
    let candidate = std::path::PathBuf::from("../scuf_virtual_pad_managed.py");
    std::fs::canonicalize(&candidate).map_err(|_| {
        "Cannot find scuf_virtual_pad_managed.py — set SCUF_PAD_PATH env var".to_string()
    })
}

fn unit_path() -> Result<std::path::PathBuf, String> {
    let home = std::env::var("HOME").map_err(|_| "HOME not set".to_string())?;
    Ok(std::path::PathBuf::from(home)
        .join(".config/systemd/user")
        .join(format!("{SERVICE_NAME}.service")))
}

fn systemctl(args: &[&str]) -> Result<std::process::Output, String> {
    Command::new("systemctl")
        .arg("--user")
        .args(args)
        .output()
        .map_err(|e| format!("systemctl error: {e}"))
}

// ── Device detection ─────────────────────────────────────────

#[tauri::command]
fn check_device() -> bool {
    let Ok(entries) = std::fs::read_dir("/sys/class/hidraw") else {
        return false;
    };
    for entry in entries.flatten() {
        let uevent = entry.path().join("device/uevent");
        if let Ok(content) = std::fs::read_to_string(uevent) {
            let lower = content.to_lowercase();
            if lower.contains("2e95") && lower.contains("434d") {
                return true;
            }
        }
    }
    false
}

// ── Udev / permissions ───────────────────────────────────────

const UDEV_RULE_PATH: &str = "/etc/udev/rules.d/99-scuf.rules";
const UDEV_RULE_HIDRAW: &str =
    r#"SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2e95", ATTRS{idProduct}=="434d", MODE="0666""#;
// ENV{ID_INPUT_JOYSTICK}="" hides the physical device from SDL2/Steam joystick enumeration.
// SDL2 checks this property; if not "1", the device is skipped.
// The virtual UInput device has its own separate udev entry and is unaffected.
const UDEV_RULE_INPUT: &str =
    r#"SUBSYSTEM=="input", ATTRS{idVendor}=="2e95", ATTRS{idProduct}=="434d", MODE="0666", ENV{ID_INPUT_JOYSTICK}="""#;

/// Returns false only when SCUF hidraw is present but not readable.
/// Returns true when device is absent (nothing to fix) or readable.
#[tauri::command]
fn hidraw_accessible() -> bool {
    let Ok(entries) = std::fs::read_dir("/sys/class/hidraw") else {
        return true;
    };
    for entry in entries.flatten() {
        let uevent = entry.path().join("device/uevent");
        if let Ok(content) = std::fs::read_to_string(uevent) {
            let lower = content.to_lowercase();
            if lower.contains("2e95") && lower.contains("434d") {
                let dev_path = std::path::PathBuf::from("/dev").join(entry.file_name());
                return std::fs::OpenOptions::new().read(true).open(&dev_path).is_ok();
            }
        }
    }
    true // no SCUF device present
}

pub const UDEV_MANUAL_CMD: &str = concat!(
    "sudo sh -c \"",
    "echo 'SUBSYSTEM==\\\"hidraw\\\", ATTRS{idVendor}==\\\"2e95\\\", ATTRS{idProduct}==\\\"434d\\\", MODE=\\\"0666\\\"'",
    " > /etc/udev/rules.d/99-scuf.rules && ",
    "echo 'SUBSYSTEM==\\\"input\\\", ATTRS{idVendor}==\\\"2e95\\\", ATTRS{idProduct}==\\\"434d\\\", MODE=\\\"0666\\\", ENV{ID_INPUT_JOYSTICK}=\\\"\\\"'",
    " >> /etc/udev/rules.d/99-scuf.rules && ",
    "udevadm control --reload-rules && udevadm trigger\""
);

/// Install udev rule via pkexec (prompts admin password once, permanent fix).
#[tauri::command]
fn install_udev() -> Result<(), String> {
    linfo!("install_udev: writing {UDEV_RULE_PATH}");
    let cmd = format!(
        "{{ printf '%s\\n' {r1}; printf '%s\\n' {r2}; }} > {path} && udevadm control --reload-rules && udevadm trigger",
        r1   = shell_escape(UDEV_RULE_HIDRAW),
        r2   = shell_escape(UDEV_RULE_INPUT),
        path = UDEV_RULE_PATH,
    );
    let out = Command::new("pkexec")
        .args(["sh", "-c", &cmd])
        .output()
        .map_err(|e| format!("pkexec: {e}"))?;
    if out.status.success() {
        linfo!("install_udev: success");
        Ok(())
    } else {
        let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
        lerror!("install_udev: pkexec failed — {msg}");
        Err(format!("MANUAL_CMD:{}", UDEV_MANUAL_CMD))
    }
}

#[tauri::command]
fn udev_manual_cmd() -> String {
    UDEV_MANUAL_CMD.to_string()
}

fn shell_escape(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

// ── Service management ───────────────────────────────────────

#[tauri::command]
fn service_installed() -> bool {
    unit_path().map(|p| p.exists()).unwrap_or(false)
}

#[tauri::command]
fn install_service() -> Result<(), String> {
    linfo!("install_service: starting");

    let script = resolve_pad_script().map_err(|e| {
        lerror!("install_service: resolve script failed: {e}");
        e
    })?;

    let unit_file = unit_path()?;
    let dir = unit_file.parent().unwrap();
    std::fs::create_dir_all(dir).map_err(|e| {
        let msg = format!("mkdir {dir:?} failed: {e}");
        lerror!("install_service: {msg}");
        msg
    })?;

    let unit = format!(
        "[Unit]\n\
         Description=SCUF Envision Pro virtual pad bridge\n\
         After=graphical-session.target\n\
         \n\
         [Service]\n\
         Type=simple\n\
         ExecStart=/usr/bin/python3 {script}\n\
         Restart=on-failure\n\
         RestartSec=3\n\
         StandardOutput=append:{GUI_LOG}\n\
         StandardError=append:{GUI_LOG}\n\
         \n\
         [Install]\n\
         WantedBy=default.target\n",
        script = script.display()
    );

    std::fs::write(&unit_file, &unit).map_err(|e| {
        let msg = format!("Write unit file failed: {e}");
        lerror!("install_service: {msg}");
        msg
    })?;

    linfo!("install_service: wrote unit to {}", unit_file.display());

    let reload = systemctl(&["daemon-reload"])?;
    if !reload.status.success() {
        let msg = format!("daemon-reload failed: {}", String::from_utf8_lossy(&reload.stderr).trim());
        lerror!("install_service: {msg}");
        return Err(msg);
    }

    let enable = systemctl(&["enable", SERVICE_NAME])?;
    if !enable.status.success() {
        let msg = format!("enable failed: {}", String::from_utf8_lossy(&enable.stderr).trim());
        lerror!("install_service: {msg}");
        return Err(msg);
    }

    linfo!("install_service: service enabled — {SERVICE_NAME}");
    Ok(())
}

#[tauri::command]
fn uninstall_service() -> Result<(), String> {
    linfo!("uninstall_service: starting");
    let _ = systemctl(&["stop", SERVICE_NAME]);
    let _ = systemctl(&["disable", SERVICE_NAME]);
    if let Ok(p) = unit_path() {
        let _ = std::fs::remove_file(&p);
        linfo!("uninstall_service: removed unit {}", p.display());
    }
    let _ = systemctl(&["daemon-reload"]);
    linfo!("uninstall_service: complete");
    Ok(())
}

#[tauri::command]
fn bridge_running() -> bool {
    systemctl(&["is-active", "--quiet", SERVICE_NAME])
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[tauri::command]
fn start_bridge() -> Result<(), String> {
    linfo!("start_bridge: invoking systemctl start {SERVICE_NAME}");
    let out = systemctl(&["start", SERVICE_NAME])?;
    if out.status.success() {
        linfo!("start_bridge: success");
        Ok(())
    } else {
        let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
        lerror!("start_bridge: failed — {msg}");
        Err(msg)
    }
}

#[tauri::command]
fn stop_bridge() -> Result<(), String> {
    linfo!("stop_bridge: invoking systemctl stop {SERVICE_NAME}");
    let out = systemctl(&["stop", SERVICE_NAME])?;
    if out.status.success() {
        linfo!("stop_bridge: success");
        Ok(())
    } else {
        let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
        lerror!("stop_bridge: failed — {msg}");
        Err(msg)
    }
}

// ── Virtual pad config ───────────────────────────────────────

#[tauri::command]
fn get_vpad_config() -> serde_json::Value {
    let path = vpad_config_path();
    if let Ok(content) = std::fs::read_to_string(&path) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&content) {
            return v;
        }
    }
    default_vpad_config()
}

#[tauri::command]
fn set_vpad_config(config: serde_json::Value) -> Result<(), String> {
    let path = vpad_config_path();
    let content = serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?;
    std::fs::write(&path, content).map_err(|e| {
        let msg = e.to_string();
        lerror!("set_vpad_config: {msg}");
        msg
    })?;
    linfo!("set_vpad_config: saved to {}", path.display());
    Ok(())
}

// ── Firmware remap ───────────────────────────────────────────

#[tauri::command]
async fn get_profiles() -> Result<Vec<ProfileData>, String> {
    let script = scuf_script();
    let output = Command::new("python3")
        .args([&script, "--json", "show"])
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("get_profiles: {stderr}");
        return Err(format!("Device error: {stderr}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim())
        .map_err(|e| format!("Parse error: {e}\nOutput: {stdout}"))
}

#[tauri::command]
async fn unmap_button(source: String, profile: u8) -> Result<String, String> {
    linfo!("unmap_button: profile={profile} {source}");
    let script = scuf_script();
    let output = Command::new("python3")
        .args([&script, "--profile", &profile.to_string(), "unmap", &source])
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("unmap_button: failed — {stderr}");
        return Err(format!("Unmap failed: {stderr}"));
    }

    let result = String::from_utf8_lossy(&output.stdout).trim().to_string();
    linfo!("unmap_button: ok — {result}");
    Ok(result)
}

#[tauri::command]
async fn remap_button(source: String, target: String, profile: u8) -> Result<String, String> {
    linfo!("remap_button: profile={profile} {source} -> {target}");
    let script = scuf_script();
    let output = Command::new("python3")
        .args([&script, "--profile", &profile.to_string(), "remap", &source, &target])
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("remap_button: failed — {stderr}");
        return Err(format!("Remap failed: {stderr}"));
    }

    let result = String::from_utf8_lossy(&output.stdout).trim().to_string();
    linfo!("remap_button: ok — {result}");
    Ok(result)
}

// ── Thumbstick presets ───────────────────────────────────────

#[tauri::command]
async fn get_presets(profile: Option<u8>) -> Result<Vec<PresetProfileData>, String> {
    let script = scuf_script();
    let mut args = vec![script, "--json".to_string(), "show-presets".to_string()];
    if let Some(p) = profile {
        args.insert(1, "--profile".to_string());
        args.insert(2, p.to_string());
    }
    let output = Command::new("python3")
        .args(&args)
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("get_presets: {stderr}");
        return Err(format!("Device error: {stderr}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim())
        .map_err(|e| format!("Parse error: {e}\nOutput: {stdout}"))
}

#[tauri::command]
async fn set_preset(
    preset_name: String,
    profile: u8,
    left_curve: Option<String>,
    right_curve: Option<String>,
    left_dz: Option<u8>,
    left_max_dz: Option<u8>,
    right_dz: Option<u8>,
    right_max_dz: Option<u8>,
) -> Result<String, String> {
    linfo!("set_preset: profile={profile} name={preset_name:?}");
    let script = scuf_script();
    let mut cmd = Command::new("python3");
    cmd.args([&script, "--profile", &profile.to_string(), "set-preset", &preset_name]);

    if let Some(ref c) = left_curve   { cmd.args(["--left-curve",   c]); }
    if let Some(ref c) = right_curve  { cmd.args(["--right-curve",  c]); }
    if let Some(v) = left_dz          { cmd.args(["--left-dz",      &v.to_string()]); }
    if let Some(v) = left_max_dz      { cmd.args(["--left-max-dz",  &v.to_string()]); }
    if let Some(v) = right_dz         { cmd.args(["--right-dz",     &v.to_string()]); }
    if let Some(v) = right_max_dz     { cmd.args(["--right-max-dz", &v.to_string()]); }

    let output = cmd.output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("set_preset: failed — {stderr}");
        return Err(format!("Set preset failed: {stderr}"));
    }

    let result = String::from_utf8_lossy(&output.stdout).trim().to_string();
    linfo!("set_preset: ok — {result}");
    Ok(result)
}

// ── Trigger presets ──────────────────────────────────────────

#[tauri::command]
async fn get_trigger_presets(profile: Option<u8>) -> Result<Vec<PresetProfileData>, String> {
    let script = scuf_script();
    let mut args = vec![script, "--json".to_string(), "show-trigger-presets".to_string()];
    if let Some(p) = profile {
        args.insert(1, "--profile".to_string());
        args.insert(2, p.to_string());
    }
    let output = Command::new("python3")
        .args(&args)
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("get_trigger_presets: {stderr}");
        return Err(format!("Device error: {stderr}"));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim())
        .map_err(|e| format!("Parse error: {e}\nOutput: {stdout}"))
}

#[tauri::command]
async fn set_trigger_preset(
    preset_name: String,
    profile: u8,
    left_curve: Option<String>,
    right_curve: Option<String>,
    left_dz: Option<u8>,
    left_max_dz: Option<u8>,
    right_dz: Option<u8>,
    right_max_dz: Option<u8>,
) -> Result<String, String> {
    linfo!("set_trigger_preset: profile={profile} name={preset_name:?}");
    let script = scuf_script();
    let mut cmd = Command::new("python3");
    cmd.args([&script, "--profile", &profile.to_string(), "set-trigger-preset", &preset_name]);

    if let Some(ref c) = left_curve   { cmd.args(["--left-curve",   c]); }
    if let Some(ref c) = right_curve  { cmd.args(["--right-curve",  c]); }
    if let Some(v) = left_dz          { cmd.args(["--left-dz",      &v.to_string()]); }
    if let Some(v) = left_max_dz      { cmd.args(["--left-max-dz",  &v.to_string()]); }
    if let Some(v) = right_dz         { cmd.args(["--right-dz",     &v.to_string()]); }
    if let Some(v) = right_max_dz     { cmd.args(["--right-max-dz", &v.to_string()]); }

    let output = cmd.output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("set_trigger_preset: failed — {stderr}");
        return Err(format!("Set trigger preset failed: {stderr}"));
    }

    let result = String::from_utf8_lossy(&output.stdout).trim().to_string();
    linfo!("set_trigger_preset: ok — {result}");
    Ok(result)
}

// ── Brightness ──────────────────────────────────────────────

#[tauri::command]
async fn set_brightness(level: u8) -> Result<String, String> {
    linfo!("set_brightness: {level}%");
    let script = scuf_script();
    let output = Command::new("python3")
        .args([&script, "brightness", &level.to_string()])
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("set_brightness: {stderr}");
        return Err(format!("Brightness failed: {stderr}"));
    }

    let result = String::from_utf8_lossy(&output.stdout).trim().to_string();
    linfo!("set_brightness: ok — {result}");
    Ok(result)
}

// ── Eco mode ────────────────────────────────────────────────

#[tauri::command]
async fn set_eco_mode(enabled: bool) -> Result<String, String> {
    linfo!("set_eco_mode: {enabled}");
    let script = scuf_script();
    let state = if enabled { "on" } else { "off" };
    let output = Command::new("python3")
        .args([&script, "eco-mode", state])
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("set_eco_mode: {stderr}");
        return Err(format!("Eco mode failed: {stderr}"));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

// ── Auto shutoff ─────────────────────────────────────────────

#[tauri::command]
async fn set_auto_shutoff(enabled: bool, minutes: Option<u8>) -> Result<String, String> {
    linfo!("set_auto_shutoff: enabled={enabled} minutes={minutes:?}");
    let script = scuf_script();
    let state = if enabled { "on" } else { "off" };
    let mut cmd = Command::new("python3");
    cmd.args([&script, "auto-shutoff", state]);
    if let Some(m) = minutes {
        cmd.args(["--minutes", &m.to_string()]);
    }
    let output = cmd.output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("set_auto_shutoff: {stderr}");
        return Err(format!("Auto shutoff failed: {stderr}"));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

// ── Vibration ────────────────────────────────────────────────

#[tauri::command]
async fn get_vibration(profile: Option<u8>) -> Result<Vec<VibrationData>, String> {
    linfo!("get_vibration: profile={profile:?}");
    let script = scuf_script();
    let mut cmd = Command::new("python3");
    cmd.args([&script, "--json", "show-vibration"]);
    if let Some(p) = profile {
        cmd.args(["--profile", &p.to_string()]);
    }
    let output = cmd.output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("get_vibration: {stderr}");
        return Err(format!("Get vibration failed: {stderr}"));
    }
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    serde_json::from_str(&stdout)
        .map_err(|e| format!("JSON parse error: {e}\nOutput: {stdout}"))
}

#[tauri::command]
async fn set_vibration(profile: u8, left: u8, right: u8) -> Result<String, String> {
    linfo!("set_vibration: profile={profile} left={left} right={right}");
    let script = scuf_script();
    let output = Command::new("python3")
        .args([
            &script,
            "--profile", &profile.to_string(),
            "set-vibration",
            "--left",  &left.to_string(),
            "--right", &right.to_string(),
        ])
        .output()
        .map_err(|e| format!("Failed to run scuf_config.py: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        lerror!("set_vibration: {stderr}");
        return Err(format!("Set vibration failed: {stderr}"));
    }
    let result = String::from_utf8_lossy(&output.stdout).trim().to_string();
    linfo!("set_vibration: ok — {result}");
    Ok(result)
}

// ── Input monitor ────────────────────────────────────────────

static MONITOR_RUNNING:    AtomicBool = AtomicBool::new(false);
static MONITOR_IS_VIRTUAL: AtomicBool = AtomicBool::new(false);

#[derive(Serialize, Clone)]
struct InputPressPayload {
    code:   String, // BTN_WEST, BTN_C, etc.
    source: String, // "virtual" | "physical"
}

#[derive(Serialize)]
struct MonitorDebug {
    virtual_path:        Option<String>,
    physical_path:       Option<String>,
    virtual_accessible:  bool,
    physical_accessible: bool,
    monitor_running:     bool,
    monitor_is_virtual:  bool,
}

/// Map virtual UInput button codes to BTN_ names.
fn virtual_btn_name(code: u16) -> Option<&'static str> {
    match code {
        0x130 => Some("BTN_SOUTH"),
        0x131 => Some("BTN_EAST"),
        0x133 => Some("BTN_NORTH"),
        0x134 => Some("BTN_WEST"),
        0x136 => Some("BTN_TL"),
        0x137 => Some("BTN_TR"),
        0x13a => Some("BTN_SELECT"),
        0x13b => Some("BTN_START"),
        0x13c => Some("BTN_MODE"),
        0x13d => Some("BTN_THUMBL"),
        0x13e => Some("BTN_THUMBR"),
        0x220 => Some("BTN_DPAD_UP"),
        0x221 => Some("BTN_DPAD_DOWN"),
        0x222 => Some("BTN_DPAD_LEFT"),
        0x223 => Some("BTN_DPAD_RIGHT"),
        _ => None,
    }
}

/// Map physical SCUF Envision Pro button codes to BTN_ names (matches VP_SOURCE_CODES).
fn physical_btn_name(code: u16) -> Option<&'static str> {
    match code {
        0x130 => Some("BTN_SOUTH"), // A
        0x131 => Some("BTN_EAST"),  // B
        0x132 => Some("BTN_C"),     // X on SCUF
        0x133 => Some("BTN_NORTH"), // Y
        0x134 => Some("BTN_WEST"),  // LB on SCUF
        0x135 => Some("BTN_Z"),     // RB on SCUF
        0x136 => Some("BTN_TL"),    // Select on SCUF
        0x137 => Some("BTN_TR"),    // Start on SCUF
        0x138 => Some("BTN_TL2"),   // L3
        0x139 => Some("BTN_TR2"),   // R3
        _ => None,
    }
}

/// Extract the first eventN number from an H: Handlers line.
/// Handles both "eventN" (separate token) and "Handlers=eventN" (combined token).
fn extract_event_from_handlers(line: &str) -> Option<String> {
    for token in line.split_whitespace() {
        // Find "event" anywhere in the token, then check remaining chars are digits
        if let Some(pos) = token.find("event") {
            let rest = &token[pos + 5..];
            if !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()) {
                return Some(format!("/dev/input/event{rest}"));
            }
        }
    }
    None
}

/// Find the virtual UInput "Virtual SCUF Envision Pro" evdev node.
fn find_virtual_scuf_evdev() -> Option<String> {
    let content = std::fs::read_to_string("/proc/bus/input/devices").ok()?;
    let mut in_target = false;
    for line in content.lines() {
        if line.starts_with("N:") {
            in_target = line.contains("Virtual SCUF Envision Pro");
        }
        if in_target && line.starts_with("H:") {
            return extract_event_from_handlers(line);
        }
    }
    None
}

/// Find the physical SCUF Envision Pro evdev node (VID 2e95, PID 434d, not virtual).
/// Prefers the node that has a js (joystick) handler — that's where button events are.
fn find_physical_scuf_evdev() -> Option<String> {
    let content = std::fs::read_to_string("/proc/bus/input/devices").ok()?;
    let mut in_target = false;
    let mut name_ok = false;
    let mut fallback: Option<String> = None;
    for line in content.lines() {
        if line.starts_with("I:") {
            let lower = line.to_lowercase();
            in_target = lower.contains("vendor=2e95") && lower.contains("product=434d");
            name_ok = false;
        }
        if in_target && line.starts_with("N:") {
            name_ok = !line.contains("Virtual");
        }
        if in_target && name_ok && line.starts_with("H:") {
            if let Some(path) = extract_event_from_handlers(line) {
                // Prefer the joystick node (has a jsN token) — that's where button events live
                if line.split_whitespace().any(|t| {
                    t.starts_with("js") && t[2..].chars().all(|c| c.is_ascii_digit())
                }) {
                    return Some(path);
                }
                if fallback.is_none() {
                    fallback = Some(path);
                }
            }
        }
    }
    fallback
}

fn monitor_loop(device_path: String, is_virtual: bool, app_handle: tauri::AppHandle) {
    let mut file = match std::fs::File::open(&device_path) {
        Ok(f) => f,
        Err(e) => {
            lerror!("monitor_loop: open {device_path}: {e}");
            MONITOR_RUNNING.store(false, Ordering::SeqCst);
            return;
        }
    };
    let source = if is_virtual { "virtual" } else { "physical" };
    linfo!("monitor_loop: listening on {device_path} ({source})");

    // Resolve the window once — emit_to a named window is more reliable from
    // background threads than AppHandle::emit (global broadcast) in Tauri v2.
    let window = app_handle.get_webview_window("main");
    if window.is_none() {
        lerror!("monitor_loop: no window 'main' found");
    }

    let emit = |event: &str, payload: serde_json::Value| {
        if let Some(w) = &window {
            match Emitter::emit(w, event, payload) {
                Ok(()) => linfo!("monitor_loop: emit ok ({event})"),
                Err(e) => lerror!("monitor_loop: emit {event} failed: {e}"),
            }
        } else {
            lerror!("monitor_loop: no window, cannot emit {event}");
        }
    };

    // 64-bit Linux input_event: [tv_sec i64, tv_usec i64, type u16, code u16, value i32] = 24 bytes
    let mut buf = [0u8; 24];
    loop {
        match file.read_exact(&mut buf) {
            Ok(()) => {
                let ev_type  = u16::from_ne_bytes([buf[16], buf[17]]);
                let ev_code  = u16::from_ne_bytes([buf[18], buf[19]]);
                let ev_value = i32::from_ne_bytes([buf[20], buf[21], buf[22], buf[23]]);
                if ev_type == 1 && ev_value == 1 {
                    let name_opt = if is_virtual {
                        virtual_btn_name(ev_code)
                    } else {
                        physical_btn_name(ev_code)
                    };
                    if let Some(name) = name_opt {
                        linfo!("monitor_loop: {} {}", source, name);
                        emit("input-press", serde_json::json!({
                            "code":   name,
                            "source": source,
                        }));
                    }
                }
            }
            Err(e) => {
                lerror!("monitor_loop: read error on {device_path}: {e}");
                break;
            }
        }
    }
    linfo!("monitor_loop: exited ({source})");
    MONITOR_RUNNING.store(false, Ordering::SeqCst);
}

#[tauri::command]
fn start_input_monitor(app_handle: tauri::AppHandle) -> Result<String, String> {
    // Already on virtual — best state, nothing to do
    if MONITOR_RUNNING.load(Ordering::SeqCst) && MONITOR_IS_VIRTUAL.load(Ordering::SeqCst) {
        return Ok("already-virtual".to_string());
    }

    // Try virtual device first (only exists when bridge is running)
    if let Some(vpath) = find_virtual_scuf_evdev() {
        if std::fs::File::open(&vpath).is_ok() {
            // Start virtual monitor (override any physical monitor that may be blocking)
            MONITOR_IS_VIRTUAL.store(true, Ordering::SeqCst);
            MONITOR_RUNNING.store(true, Ordering::SeqCst);
            linfo!("start_input_monitor: starting virtual {vpath}");
            let path = vpath.clone();
            std::thread::spawn(move || monitor_loop(path, true, app_handle));
            return Ok(format!("started-virtual:{vpath}"));
        }
    }

    // Virtual not available. Try physical only if nothing is running.
    if MONITOR_RUNNING.load(Ordering::SeqCst) {
        return Ok("already-physical".to_string());
    }
    if MONITOR_RUNNING.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
        return Ok("already-running".to_string());
    }

    if let Some(ppath) = find_physical_scuf_evdev() {
        if std::fs::File::open(&ppath).is_ok() {
            MONITOR_IS_VIRTUAL.store(false, Ordering::SeqCst);
            linfo!("start_input_monitor: starting physical {ppath}");
            let path = ppath.clone();
            std::thread::spawn(move || monitor_loop(path, false, app_handle));
            return Ok(format!("started-physical:{ppath}"));
        } else {
            MONITOR_RUNNING.store(false, Ordering::SeqCst);
            return Err(format!(
                "Physical device {ppath} not accessible — use 'Fix Permissions' in app"
            ));
        }
    }

    MONITOR_RUNNING.store(false, Ordering::SeqCst);
    Err("No SCUF input device found — start bridge or check connection".to_string())
}

#[tauri::command]
fn get_monitor_debug() -> MonitorDebug {
    let virtual_path  = find_virtual_scuf_evdev();
    let physical_path = find_physical_scuf_evdev();
    let virtual_accessible  = virtual_path.as_ref()
        .map(|p| std::fs::File::open(p).is_ok()).unwrap_or(false);
    let physical_accessible = physical_path.as_ref()
        .map(|p| std::fs::File::open(p).is_ok()).unwrap_or(false);
    MonitorDebug {
        virtual_path,
        physical_path,
        virtual_accessible,
        physical_accessible,
        monitor_running:    MONITOR_RUNNING.load(Ordering::SeqCst),
        monitor_is_virtual: MONITOR_IS_VIRTUAL.load(Ordering::SeqCst),
    }
}

// ── Main ─────────────────────────────────────────────────────

fn main() {
    linfo!("scuf-gui starting — log: {GUI_LOG}");

    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            check_device,
            hidraw_accessible,
            install_udev,
            udev_manual_cmd,
            start_input_monitor,
            get_monitor_debug,
            service_installed,
            install_service,
            uninstall_service,
            bridge_running,
            start_bridge,
            stop_bridge,
            get_vpad_config,
            set_vpad_config,
            get_profiles,
            remap_button,
            unmap_button,
            get_presets,
            set_preset,
            get_trigger_presets,
            set_trigger_preset,
            set_brightness,
            set_eco_mode,
            set_auto_shutoff,
            get_vibration,
            set_vibration,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
