from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pyperclip
import webview

# ── helpers ──────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_OS = platform.system()   # "Windows" | "Darwin" | "Linux"


# ── System metrics collector ─────────────────────────────────────────────────

class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0
        self.gpu  = -1.0
        self.tmp  = -1.0
        self._lock = threading.Lock()
        self._last_net   = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running    = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        net = ((nc.bytes_sent - self._last_net.bytes_sent) +
               (nc.bytes_recv - self._last_net.bytes_recv)) / max(dt, 0.01) / (1024 * 1024)
        self._last_net   = nc
        self._last_net_t = now
        gpu = self._get_gpu()
        tmp = self._get_temp()
        with self._lock:
            self.cpu = cpu; self.mem = mem; self.net = net
            self.gpu = gpu; self.tmp = tmp

    def _get_gpu(self) -> float:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass
        return -1.0

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz", "zenpower"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi).CurrentTemperature"],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    return (float(r.stdout.strip().split("\n")[0]) / 10.0) - 273.15
            except Exception:
                pass
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {"cpu": self.cpu, "mem": self.mem, "net": self.net,
                    "gpu": self.gpu, "tmp": self.tmp}


_metrics = _SysMetrics()


# ── HTML/CSS/JS template ──────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A.R.I.S</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  :root{
    --bg:#00060a;--panel:#010d14;--panel2:#010f18;--border:#0d3347;
    --border-b:#1a5c7a;--border-a:#0f4060;--pri:#00d4ff;--pri-dim:#007a99;
    --pri-gho:#001f2e;--acc:#ff6b00;--acc2:#ffcc00;--green:#00ff88;
    --red:#ff3355;--muted:#ff3366;--text:#8ffcff;--text-dim:#3a8a9a;
    --text-med:#5ab8cc;--white:#d8f8ff;--dark:#000d14;--bar-bg:#011520;
  }
  body{
    background:var(--bg);color:var(--text);
    font-family:'Courier New',Courier,monospace;
    height:100vh;overflow:hidden;display:flex;flex-direction:column;
    user-select:none;
  }

  /* ── HEADER ── */
  .hdr{
    display:flex;align-items:center;justify-content:space-between;
    height:54px;background:var(--dark);border-bottom:1px solid var(--border-b);
    padding:0 16px;flex-shrink:0;
  }
  .hdr-left{display:flex;align-items:center;gap:10px}
  .hdr-badge{font-size:8px;color:var(--pri-dim);letter-spacing:1px}
  .hdr-center{text-align:center}
  .hdr-title{
    font-size:18px;font-weight:bold;color:var(--pri);letter-spacing:4px;
    animation:glow-pulse 3s ease-in-out infinite;
  }
  @keyframes glow-pulse{
    0%,100%{text-shadow:0 0 10px rgba(0,212,255,.5)}
    50%{text-shadow:0 0 28px rgba(0,212,255,.95),0 0 50px rgba(0,212,255,.35)}
  }
  .hdr-subtitle{font-size:7px;color:var(--pri-dim);letter-spacing:2px}
  .hdr-right{text-align:right}
  #clock{font-size:15px;font-weight:bold;color:var(--pri);letter-spacing:2px}
  #date-lbl{font-size:7px;color:var(--text-dim)}

  /* ── BODY ── */
  .body{display:flex;flex:1;overflow:hidden}

  /* ── LEFT PANEL ── */
  .left{
    width:148px;flex-shrink:0;background:var(--dark);
    border-right:1px solid var(--border);padding:10px 8px;
    display:flex;flex-direction:column;gap:6px;overflow:hidden;
  }
  .sec-title{
    font-size:7px;font-weight:bold;color:var(--pri);
    border-bottom:1px solid var(--border);padding-bottom:4px;letter-spacing:1px;
  }
  .mbar{
    background:var(--panel2);border:1px solid var(--border-a);
    border-radius:4px;height:38px;padding:5px 6px;position:relative;overflow:hidden;
  }
  .mbar .lbl{font-size:7px;font-weight:bold;color:var(--text-dim)}
  .mbar .val{
    font-size:9px;font-weight:bold;position:absolute;right:6px;top:4px;
    transition:color .3s;
  }
  .mbar .track{
    position:absolute;bottom:5px;left:6px;right:6px;height:4px;
    background:var(--bar-bg);border-radius:2px;
  }
  .mbar .fill{height:100%;border-radius:2px;transition:width .5s ease,background .3s}
  .sysbox{
    background:var(--panel2);border:1px solid var(--border);
    border-radius:4px;padding:5px 6px;display:flex;flex-direction:column;gap:3px;
  }
  .sysbox div{font-size:8px;font-weight:bold}
  .spacer{flex:1}
  .sbadge{
    text-align:center;font-size:7px;font-weight:bold;
    border:1px solid var(--border-a);border-radius:3px;padding:4px;
  }

  /* ── CENTER ── */
  .center{
    flex:1;display:flex;align-items:center;justify-content:center;
    background:var(--bg);position:relative;overflow:hidden;
  }
  #hud{width:100%;height:100%}

  /* ── RIGHT PANEL ── */
  .right{
    width:340px;flex-shrink:0;background:var(--dark);
    border-left:1px solid var(--border);padding:8px;
    display:flex;flex-direction:column;gap:6px;overflow:hidden;
  }
  .log{
    flex:1;overflow-y:auto;background:var(--panel);
    border:1px solid var(--border);border-radius:4px;padding:6px;
    font-size:9px;min-height:0;
  }
  .log::-webkit-scrollbar{width:6px}
  .log::-webkit-scrollbar-track{background:var(--bg)}
  .log::-webkit-scrollbar-thumb{background:var(--border-b);border-radius:3px}
  .le{margin-bottom:2px;word-break:break-word;line-height:1.4}
  .le.you{color:var(--white)}
  .le.aris{color:var(--pri)}
  .le.err{color:var(--red)}
  .le.file{color:var(--green)}
  .le.sys{color:var(--acc2)}
  .le.cur::after{content:'▌';animation:blink .5s step-end infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
  .log-tools{display:flex;gap:6px}
  .tool-btn{
    flex:1;height:26px;background:var(--panel2);color:var(--text-med);
    border:1px solid var(--border);border-radius:3px;cursor:pointer;
    font-family:'Courier New',Courier,monospace;font-size:8px;font-weight:bold;
    letter-spacing:1px;transition:all .2s;outline:none;
  }
  .tool-btn:hover{color:var(--pri);border-color:var(--border-b);box-shadow:0 0 8px rgba(0,212,255,.2)}
  .sep{height:1px;background:var(--border);margin:2px 0}

  /* ── DROP ZONE ── */
  .dz{
    height:90px;border:1.5px dashed var(--border);border-radius:6px;
    background:var(--panel);display:flex;flex-direction:column;
    align-items:center;justify-content:center;cursor:pointer;
    transition:all .2s;position:relative;overflow:hidden;
  }
  .dz:hover{border-color:var(--border-b);background:#001218}
  .dz.has-file{border-color:var(--green);background:#001a0d}
  .dz.drag-over{border-color:var(--pri);background:#001a24}
  .dz-idle{display:flex;flex-direction:column;align-items:center;gap:2px}
  .dz-icon{font-size:16px}
  .dz-txt{font-size:8px;color:var(--text-dim)}
  .dz-types{font-size:7px;color:#1a4a5a}
  .dz-finfo{
    width:100%;padding:0 10px;display:flex;align-items:center;gap:8px;
  }
  .dz-fname{color:var(--white);font-size:8px;font-weight:bold;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
  .dz-fext{color:var(--text-dim);font-size:7px}
  .dz-clr{color:var(--red);font-size:16px;cursor:pointer;opacity:.7;flex-shrink:0}
  .dz-clr:hover{opacity:1}
  .fhint{font-size:7px;color:var(--text-med);word-break:break-word}

  /* ── INPUT ── */
  .irow{display:flex;gap:5px}
  #ci{
    flex:1;background:#000d14;color:var(--white);
    border:1px solid var(--border);border-radius:3px;
    padding:3px 7px;font-family:'Courier New',Courier,monospace;
    font-size:9px;height:30px;outline:none;transition:border-color .2s;
  }
  #ci:focus{border-color:var(--pri);box-shadow:0 0 8px rgba(0,212,255,.2)}
  #ci::placeholder{color:var(--text-dim)}
  #sb{
    width:30px;height:30px;background:var(--panel);color:var(--pri);
    border:1px solid var(--pri-dim);border-radius:3px;cursor:pointer;
    font-size:12px;font-weight:bold;transition:all .2s;
  }
  #sb:hover{background:var(--pri-gho);border-color:var(--pri);box-shadow:0 0 8px rgba(0,212,255,.3)}
  .btn{
    height:30px;border-radius:3px;cursor:pointer;
    font-family:'Courier New',Courier,monospace;font-size:8px;font-weight:bold;
    border:1px solid;transition:all .2s;outline:none;
  }
  #mb.on{background:#00140a;color:var(--green);border-color:var(--green);
    box-shadow:0 0 6px rgba(0,255,136,.2)}
  #mb.on:hover{background:#001f10}
  #mb.off{background:#140006;color:var(--muted);border-color:var(--muted);
    box-shadow:0 0 6px rgba(255,51,102,.3)}
  #fsb{background:transparent;color:var(--text-med);border-color:var(--border);height:26px}
  #fsb:hover{color:var(--pri);border-color:var(--border-b)}

  /* ── FOOTER ── */
  .footer{
    height:22px;background:var(--dark);border-top:1px solid var(--border);
    display:flex;align-items:center;justify-content:space-between;
    padding:0 14px;font-size:7px;color:var(--text-med);flex-shrink:0;
  }
  .footer .cred{color:var(--acc2);font-weight:bold}
  .footer .copy{color:var(--pri-dim)}

  /* ── SETUP OVERLAY ── */
  .ovl{
    position:fixed;top:0;left:0;right:0;bottom:0;
    background:rgba(0,6,10,.92);display:flex;align-items:center;
    justify-content:center;z-index:100;
  }
  .ovl.hide{display:none}
  .sbox{
    background:rgba(1,13,20,.97);border:1px solid var(--border-b);
    border-radius:8px;padding:32px;width:460px;max-width:95vw;
    animation:fadein .3s ease;
  }
  @keyframes fadein{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
  .stitle{font-size:13px;font-weight:bold;color:var(--pri);text-align:center;
    margin-bottom:6px;letter-spacing:2px}
  .ssub{font-size:9px;color:var(--pri-dim);text-align:center;margin-bottom:16px}
  .sdiv{border:none;border-top:1px solid var(--border);margin:12px 0}
  .slbl{font-size:8px;color:var(--text-dim);margin-bottom:4px;letter-spacing:1px}
  .sinp{
    width:100%;background:#000d12;color:var(--text);
    border:1px solid var(--border);border-radius:3px;
    padding:4px 8px;font-family:'Courier New',Courier,monospace;
    font-size:10px;height:32px;outline:none;margin-bottom:12px;transition:border-color .2s;
  }
  .sinp:focus{border-color:var(--pri)}
  .sinp.err{border-color:var(--red)}
  .osrow{display:flex;gap:6px;margin-bottom:16px}
  .osbtn{
    flex:1;height:32px;background:#000d12;color:var(--text-dim);
    border:1px solid var(--border);border-radius:3px;cursor:pointer;
    font-family:'Courier New',Courier,monospace;font-size:9px;font-weight:bold;
    transition:all .2s;
  }
  .osbtn:hover{color:var(--text);border-color:var(--border-b)}
  .osbtn.win{background:var(--pri);color:#001a22;border:none}
  .osbtn.mac{background:var(--acc2);color:#1a1400;border:none}
  .osbtn.lin{background:var(--green);color:#001a0d;border:none}
  .sinitbtn{
    width:100%;height:36px;background:transparent;color:var(--pri);
    border:1px solid var(--pri-dim);border-radius:3px;cursor:pointer;
    font-family:'Courier New',Courier,monospace;font-size:10px;font-weight:bold;
    letter-spacing:2px;transition:all .2s;
  }
  .sinitbtn:hover{background:var(--pri-gho);border-color:var(--pri);
    box-shadow:0 0 16px rgba(0,212,255,.3)}

  /* ── ATMOSPHERE ── */
  .scanlines{
    position:fixed;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:50;
    background:repeating-linear-gradient(0deg,transparent,transparent 2px,
      rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 4px);
  }
  .vignette{
    position:fixed;top:0;left:0;right:0;bottom:0;pointer-events:none;z-index:49;
    background:radial-gradient(ellipse at center,transparent 60%,rgba(0,0,0,.4) 100%);
  }
</style>
</head>
<body>
<div class="scanlines"></div>
<div class="vignette"></div>

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-left">
    <span class="hdr-badge">MARK I</span>
  </div>
  <div class="hdr-center">
    <div class="hdr-title">A.R.I.S</div>
    <div class="hdr-subtitle">ADAPTIVE RESPONSE INTELLIGENCE SYSTEM</div>
  </div>
  <div class="hdr-right">
    <div id="clock">00:00:00</div>
    <div id="date-lbl">MON 01 JAN 2026</div>
  </div>
</div>

<!-- BODY -->
<div class="body">

  <!-- LEFT: System Monitor -->
  <div class="left">
    <div class="sec-title">◈ SYS MONITOR</div>
    <div class="mbar">
      <span class="lbl">CPU</span>
      <span class="val" id="cpu-v" style="color:var(--pri)">--%</span>
      <div class="track"><div class="fill" id="cpu-f" style="background:var(--pri);width:0%"></div></div>
    </div>
    <div class="mbar">
      <span class="lbl">MEM</span>
      <span class="val" id="mem-v" style="color:var(--acc2)">--%</span>
      <div class="track"><div class="fill" id="mem-f" style="background:var(--acc2);width:0%"></div></div>
    </div>
    <div class="mbar">
      <span class="lbl">NET</span>
      <span class="val" id="net-v" style="color:var(--green)">--</span>
      <div class="track"><div class="fill" id="net-f" style="background:var(--green);width:0%"></div></div>
    </div>
    <div class="mbar">
      <span class="lbl">GPU</span>
      <span class="val" id="gpu-v" style="color:var(--acc)">--</span>
      <div class="track"><div class="fill" id="gpu-f" style="background:var(--acc);width:0%"></div></div>
    </div>
    <div class="mbar">
      <span class="lbl">TMP</span>
      <span class="val" id="tmp-v" style="color:#ff6688">--</span>
      <div class="track"><div class="fill" id="tmp-f" style="background:#ff6688;width:0%"></div></div>
    </div>
    <div class="sysbox">
      <div id="up-lbl" style="color:var(--green)">UP  --:--</div>
      <div id="pr-lbl" style="color:var(--text-med)">PROC  --</div>
      <div id="os-lbl" style="color:var(--acc2)">OS  --</div>
    </div>
    <div class="spacer"></div>
    <div class="sbadge" style="color:var(--green);background:var(--panel2)">AI CORE<br>ACTIVE</div>
    <div class="sbadge" style="color:var(--pri);background:var(--panel2)">SEC<br>CLEARED</div>
    <div class="sbadge" style="color:var(--text-dim);background:var(--panel2)">PROTOCOL<br>I</div>
  </div>

  <!-- CENTER: HUD Canvas -->
  <div class="center">
    <canvas id="hud"></canvas>
  </div>

  <!-- RIGHT: Log + File + Input -->
  <div class="right">
    <div class="sec-title">▸ ACTIVITY LOG</div>
    <div class="log" id="log"></div>
    <div class="log-tools">
      <button class="tool-btn" onclick="clearLog()" aria-label="Clear activity log">CLEAR</button>
      <button class="tool-btn" onclick="copyLog()" aria-label="Copy activity log">COPY</button>
      <button class="tool-btn" onclick="focusInput()" aria-label="Focus command input">FOCUS</button>
    </div>

    <div class="sep"></div>

    <div class="sec-title">▸ FILE UPLOAD</div>
    <div class="dz" id="dz"
         onclick="dzClick()"
         ondragover="dzDragOver(event)"
         ondragleave="dzDragLeave(event)"
         ondrop="dzDrop(event)">
      <div class="dz-idle" id="dz-idle">
        <div class="dz-icon">⬆</div>
        <div class="dz-txt">Click to Browse</div>
        <div class="dz-types">Images · Video · Audio · PDF · Docs · Code · Data</div>
      </div>
      <div class="dz-finfo" id="dz-file" style="display:none">
        <span id="f-icon" style="font-size:22px">📎</span>
        <div style="flex:1;min-width:0">
          <div id="f-name" class="dz-fname"></div>
          <div id="f-ext"  class="dz-fext"></div>
        </div>
        <span class="dz-clr" onclick="clearFile(event)">✕</span>
      </div>
    </div>
    <div class="fhint" id="fhint">No file loaded — click above to upload</div>

    <div class="sep"></div>

    <div class="sec-title">▸ COMMAND INPUT</div>
    <div class="irow">
      <input id="ci" type="text" placeholder="Type a command or question…" autocomplete="off">
      <button id="sb" onclick="sendCmd()">▸</button>
    </div>
    <button id="mb" class="btn on" onclick="toggleMute()">🎙  MICROPHONE ACTIVE</button>
    <button id="fsb" class="btn" onclick="toggleFS()">⛶  FULLSCREEN  [F11]</button>
  </div>
</div>

<!-- FOOTER -->
<div class="footer">
  <span>[F4] Mute · [F11] Fullscreen · [Ctrl+Shift+L] Clear Log · [Ctrl+Shift+K] Focus</span>
  <span class="cred">A.R.I.S — Made by AlienFromMars</span>
  <span class="copy">© 2026 AlienFromMars Industries</span>
</div>

<!-- SETUP OVERLAY -->
<div class="ovl hide" id="ovl">
  <div class="sbox">
    <div class="stitle">◈  INITIALISATION REQUIRED</div>
    <div class="ssub">Configure A.R.I.S before first boot.</div>
    <hr class="sdiv">
    <div class="slbl">GEMINI API KEY</div>
    <input type="password" class="sinp" id="gkey" placeholder="AIza…">
    <div class="slbl">OPENROUTER API KEY</div>
    <input type="password" class="sinp" id="orkey" placeholder="sk-or-…">
    <hr class="sdiv">
    <div class="slbl">OPERATING SYSTEM</div>
    <div id="os-det" style="font-size:8px;color:var(--acc2);margin-bottom:6px"></div>
    <div class="osrow">
      <button class="osbtn" id="os-w" onclick="selOS_('windows')">⊞  Windows</button>
      <button class="osbtn" id="os-m" onclick="selOS_('mac')">  macOS</button>
      <button class="osbtn" id="os-l" onclick="selOS_('linux')">🐧  Linux</button>
    </div>
    <button class="sinitbtn" onclick="submitSetup()">▸  INITIALISE SYSTEMS</button>
  </div>
</div>

<script>
// =====================================================================
//  A.R.I.S — Adaptive Response Intelligence System
//  UI Controller  |  Made by AlienFromMars
// =====================================================================

// ── State ──────────────────────────────────────────────────────────────
const S = {
  mode:'INITIALISING', speaking:false, muted:false,
  tick:0, scale:1.0, targetScale:1.0,
  halo:55, targetHalo:55,
  rings:[0,120,240], scan:0, scan2:180,
  pulses:[0,50,100], particles:[],
  blink:true, blinkTick:0, lastT:0
};

const C = {
  bg:'#00060a', pri:'#00d4ff', priDim:'#007a99', priGho:'#001f2e',
  acc:'#ff6b00', acc2:'#ffcc00', grn:'#00ff88', red:'#ff3355',
  muted:'#ff3366', textDim:'#3a8a9a', textMed:'#5ab8cc', white:'#d8f8ff',
  borderB:'#1a5c7a'
};

function h2r(hex,a){const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);return`rgba(${r},${g},${b},${a})`}
function lerp(a,b,t){return a+(b-a)*t}

function stateColor(){
  if(S.muted) return C.muted;
  if(S.speaking) return C.acc;
  if(S.mode==='THINKING'||S.mode==='PROCESSING') return C.acc2;
  if(S.mode==='LISTENING') return C.grn;
  return C.pri;
}

// ── Canvas HUD ─────────────────────────────────────────────────────────
const cv = document.getElementById('hud');
const cx2= cv.getContext('2d');

function resize(){
  const el=document.querySelector('.center');
  cv.width=el.clientWidth; cv.height=el.clientHeight;
}
window.addEventListener('resize',resize); resize();

function drawHUD(){
  const W=cv.width, H=cv.height, cx=W/2, cy=H/2, fw=Math.min(W,H);
  const col=stateColor();
  const hal=S.halo;

  cx2.fillStyle=C.bg; cx2.fillRect(0,0,W,H);

  // grid dots
  cx2.fillStyle=h2r(C.priGho,0.6);
  for(let x=0;x<W;x+=48) for(let y=0;y<H;y+=48) cx2.fillRect(x,y,1,1);

  const rf=fw*0.31;

  // halo glow rings
  for(let i=0;i<10;i++){
    const r=rf*(1.8-i*0.08), frc=1-i/10;
    const a=Math.max(0,Math.min(1,hal*0.085*frc/255));
    cx2.strokeStyle=h2r(col,a); cx2.lineWidth=1.5;
    cx2.beginPath(); cx2.arc(cx,cy,r,0,Math.PI*2); cx2.stroke();
  }

  // pulse rings
  const plim=fw*0.74;
  for(const pr of S.pulses){
    const a=Math.max(0,(230/255)*(1-pr/plim));
    cx2.strokeStyle=h2r(col,a); cx2.lineWidth=1.5;
    cx2.beginPath(); cx2.arc(cx,cy,pr,0,Math.PI*2); cx2.stroke();
  }

  // spinning arcs
  [[.48,3,115,78],[.40,2,78,55],[.32,1,56,40]].forEach(([rf2,lw,al,gap],idx)=>{
    const rr=fw*rf2, base=S.rings[idx];
    const a=Math.max(0,Math.min(1,hal*(1-idx*0.18)/255));
    cx2.strokeStyle=h2r(col,a); cx2.lineWidth=lw;
    let ang=base;
    while(ang<base+360){
      cx2.beginPath();
      cx2.arc(cx,cy,rr,(ang*Math.PI/180),((ang+al)*Math.PI/180));
      cx2.stroke(); ang+=al+gap;
    }
  });

  // scanners
  const sr=fw*0.50, sa=Math.min(1,hal*1.5/255), ex=S.speaking?75:44;
  cx2.strokeStyle=h2r(col,sa); cx2.lineWidth=2.5;
  cx2.beginPath();
  cx2.arc(cx,cy,sr,S.scan*Math.PI/180,(S.scan+ex)*Math.PI/180); cx2.stroke();
  cx2.strokeStyle=h2r(C.acc,sa*.5); cx2.lineWidth=1.5;
  cx2.beginPath();
  cx2.arc(cx,cy,sr,S.scan2*Math.PI/180,(S.scan2+ex)*Math.PI/180); cx2.stroke();

  // tick marks
  const tO=fw*0.497, tI=fw*0.474;
  cx2.strokeStyle=h2r(C.pri,0.55); cx2.lineWidth=1;
  for(let d=0;d<360;d+=10){
    const r=d*Math.PI/180, inn=d%30===0?tI:tI+6;
    cx2.beginPath();
    cx2.moveTo(cx+tO*Math.cos(r),cy-tO*Math.sin(r));
    cx2.lineTo(cx+inn*Math.cos(r),cy-inn*Math.sin(r));
    cx2.stroke();
  }

  // crosshair
  const chR=fw*0.51, gH=fw*0.16;
  cx2.strokeStyle=h2r(C.pri,hal*0.5/255); cx2.lineWidth=1;
  cx2.beginPath();
  cx2.moveTo(cx-chR,cy); cx2.lineTo(cx-gH,cy);
  cx2.moveTo(cx+gH,cy);  cx2.lineTo(cx+chR,cy);
  cx2.moveTo(cx,cy-chR); cx2.lineTo(cx,cy-gH);
  cx2.moveTo(cx,cy+gH);  cx2.lineTo(cx,cy+chR);
  cx2.stroke();

  // corner brackets
  const bl=24,hl=cx-fw/2,hr=cx+fw/2,ht=cy-fw/2,hb=cy+fw/2;
  cx2.strokeStyle=h2r(C.pri,0.82); cx2.lineWidth=2;
  [[hl,ht,1,1],[hr,ht,-1,1],[hl,hb,1,-1],[hr,hb,-1,-1]].forEach(([bx,by,dx,dy])=>{
    cx2.beginPath();
    cx2.moveTo(bx,by); cx2.lineTo(bx+dx*bl,by);
    cx2.moveTo(bx,by); cx2.lineTo(bx,by+dy*bl);
    cx2.stroke();
  });

  // central glow orb
  const orbR=fw*0.27*S.scale;
  const gr=cx2.createRadialGradient(cx,cy,0,cx,cy,orbR*1.4);
  if(S.muted){
    gr.addColorStop(0,`rgba(200,0,50,${hal*0.5/255})`);
    gr.addColorStop(1,'rgba(0,0,0,0)');
  } else {
    gr.addColorStop(0,`rgba(0,212,255,${hal*0.22/255})`);
    gr.addColorStop(0.5,`rgba(0,90,140,${hal*0.12/255})`);
    gr.addColorStop(1,'rgba(0,0,0,0)');
  }
  cx2.beginPath(); cx2.arc(cx,cy,orbR*1.4,0,Math.PI*2);
  cx2.fillStyle=gr; cx2.fill();

  // ARIS text
  const fs=Math.max(13,fw*0.065);
  cx2.font=`bold ${fs}px 'Courier New',monospace`;
  cx2.textAlign='center'; cx2.textBaseline='middle';
  const ta=Math.min(1,hal*2/255);
  cx2.fillStyle=h2r(col,ta);
  cx2.fillText('A.R.I.S',cx,cy);
  const tw2=cx2.measureText('A.R.I.S').width;
  cx2.strokeStyle=h2r(col,ta*0.4); cx2.lineWidth=1;
  cx2.beginPath();
  cx2.moveTo(cx-tw2/2,cy+fs*0.75); cx2.lineTo(cx+tw2/2,cy+fs*0.75);
  cx2.stroke();
  const sfs=Math.max(6,fw*0.024);
  cx2.font=`${sfs}px 'Courier New',monospace`;
  cx2.fillStyle=h2r(C.textDim,0.55);
  cx2.fillText('ADAPTIVE INTELLIGENCE',cx,cy+fs*1.35);

  // particles
  for(const pt of S.particles){
    cx2.fillStyle=h2r(C.pri,Math.max(0,Math.min(1,pt[4])));
    cx2.beginPath(); cx2.arc(pt[0],pt[1],2.5,0,Math.PI*2); cx2.fill();
  }

  // status text
  const sy=cy+fw*0.40;
  let stxt, scol;
  if(S.muted){stxt='⊘  MUTED';scol=C.muted;}
  else if(S.speaking){stxt='●  SPEAKING';scol=C.acc;}
  else if(S.mode==='THINKING'){stxt=(S.blink?'◈':'◇')+'  THINKING';scol=C.acc2;}
  else if(S.mode==='PROCESSING'){stxt=(S.blink?'▷':'▶')+'  PROCESSING';scol=C.acc2;}
  else if(S.mode==='LISTENING'){stxt=(S.blink?'●':'○')+'  LISTENING';scol=C.grn;}
  else{stxt=(S.blink?'●':'○')+'  '+S.mode;scol=C.pri;}
  cx2.font=`bold ${Math.max(9,fw*0.032)}px 'Courier New',monospace`;
  cx2.fillStyle=scol; cx2.fillText(stxt,cx,sy);

  // waveform
  const wy=sy+30, N=36, bw=8, wx0=cx-(N*bw)/2;
  for(let i=0;i<N;i++){
    let hgt, bc;
    if(S.muted){hgt=2;bc=h2r(C.muted,.7);}
    else if(S.speaking){hgt=Math.floor(Math.random()*17)+3;bc=hgt>12?h2r(C.pri,.9):h2r(C.priDim,.6);}
    else{hgt=Math.floor(3+2*Math.sin(S.tick*0.09+i*0.6));bc=h2r(C.borderB,.8);}
    cx2.fillStyle=bc;
    cx2.fillRect(wx0+i*bw,wy+20-hgt,bw-1,hgt);
  }
}

function anim(){
  requestAnimationFrame(anim);
  const now=performance.now()/1000;
  S.lastT=S.lastT||now;
  S.tick++;

  if(now-(S._lst||0)>(S.speaking?.12:.5)){
    if(S.speaking){S.targetScale=1.06+Math.random()*.08;S.targetHalo=145+Math.random()*45;}
    else if(S.muted){S.targetScale=.998+Math.random()*.004;S.targetHalo=15+Math.random()*13;}
    else{S.targetScale=1.001+Math.random()*.007;S.targetHalo=48+Math.random()*20;}
    S._lst=now;
  }
  const sp=S.speaking?.38:.15;
  S.scale+=(S.targetScale-S.scale)*sp;
  S.halo+=(S.targetHalo-S.halo)*sp;

  const spd=S.speaking?[1.3,-0.9,2.0]:[.55,-.35,.9];
  S.rings=S.rings.map((r,i)=>(r+spd[i]+360)%360);
  S.scan=(S.scan+(S.speaking?3:1.3))%360;
  S.scan2=(S.scan2+(S.speaking?-2:-.75)+360)%360;

  const fw=Math.min(cv.width,cv.height);
  const pl=fw*0.74, ps=S.speaking?4.2:2.0;
  S.pulses=S.pulses.map(r=>r+ps).filter(r=>r<pl);
  if(S.pulses.length<3&&Math.random()<(S.speaking?.07:.025)) S.pulses.push(0);

  if(S.speaking&&Math.random()<0.28){
    const ang=Math.random()*Math.PI*2, rs=fw*0.28;
    S.particles.push([cv.width/2+Math.cos(ang)*rs,cv.height/2+Math.sin(ang)*rs,
      Math.cos(ang)*(.9+Math.random()*1.5),Math.sin(ang)*(.9+Math.random()*1.5)-.4,1.0]);
  }
  S.particles=S.particles.map(p=>[p[0]+p[2],p[1]+p[3],p[2]*.97,p[3]*.97,p[4]-.028])
    .filter(p=>p[4]>0);

  if(++S.blinkTick>=38){S.blink=!S.blink;S.blinkTick=0;}
  drawHUD();
}
anim();

// ── Clock ───────────────────────────────────────────────────────────────
function tick(){
  const n=new Date();
  const hh=String(n.getHours()).padStart(2,'0'),
        mm=String(n.getMinutes()).padStart(2,'0'),
        ss=String(n.getSeconds()).padStart(2,'0');
  document.getElementById('clock').textContent=`${hh}:${mm}:${ss}`;
  const days=['SUN','MON','TUE','WED','THU','FRI','SAT'];
  const mons=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  document.getElementById('date-lbl').textContent=
    `${days[n.getDay()]} ${String(n.getDate()).padStart(2,'0')} ${mons[n.getMonth()]} ${n.getFullYear()}`;
}
tick(); setInterval(tick,1000);

// ── State ───────────────────────────────────────────────────────────────
function updateState(state){
  S.mode=state; S.speaking=(state==='SPEAKING'); S.muted=(state==='MUTED');
  const mb=document.getElementById('mb');
  if(S.muted){mb.textContent='🔇  MICROPHONE MUTED';mb.className='btn off';}
  else{mb.textContent='🎙  MICROPHONE ACTIVE';mb.className='btn on';}
}

// ── Metrics ─────────────────────────────────────────────────────────────
function setBar(id,pct,txt,col){
  const vc=pct>85?'#ff3355':pct>65?'#ff6b00':col;
  document.getElementById(id+'-v').textContent=txt;
  document.getElementById(id+'-v').style.color=vc;
  document.getElementById(id+'-f').style.width=pct+'%';
  document.getElementById(id+'-f').style.background=vc;
}
function updateMetrics(d){
  const cpu=d.cpu||0; setBar('cpu',cpu,cpu.toFixed(0)+'%','#00d4ff');
  const mem=d.mem||0; setBar('mem',mem,mem.toFixed(0)+'%','#ffcc00');
  const net=d.net||0;
  const ns=net<1?(net*1024).toFixed(0)+'KB/s':net.toFixed(1)+'MB/s';
  setBar('net',Math.min(100,net*10),ns,'#00ff88');
  const gpu=d.gpu; if(gpu>=0) setBar('gpu',gpu,gpu.toFixed(0)+'%','#ff6b00');
  else{document.getElementById('gpu-v').textContent='N/A';}
  const tmp=d.tmp; if(tmp>=0){
    const tc=tmp>80?'#ff3355':tmp>65?'#ff6b00':'#ff6688';
    setBar('tmp',Math.min(100,tmp),tmp.toFixed(0)+'°C',tc);
  } else{document.getElementById('tmp-v').textContent='N/A';}
  if(d.uptime) document.getElementById('up-lbl').textContent=d.uptime;
  if(d.proc_count) document.getElementById('pr-lbl').textContent='PROC  '+d.proc_count;
  if(d.os_name) document.getElementById('os-lbl').textContent='OS  '+d.os_name;
}

// ── Log ─────────────────────────────────────────────────────────────────
let lq=[], typing=false;
function logCls(t){
  const tl=t.toLowerCase();
  if(tl.startsWith('you:')) return 'you';
  // 'jarvis:' kept for backward-compat with any existing log/memory entries
  if(tl.startsWith('aris:')||tl.startsWith('jarvis:')) return 'aris';
  if(tl.startsWith('file:')) return 'file';
  if(tl.includes('err')) return 'err';
  return 'sys';
}
function appendLog(text){lq.push(text);if(!typing)nextLog();}
function nextLog(){
  if(!lq.length){typing=false;return;}
  typing=true;
  const text=lq.shift(), cls=logCls(text);
  const el=document.createElement('div');
  el.className='le '+cls+' cur';
  const la=document.getElementById('log');
  la.appendChild(el); la.scrollTop=la.scrollHeight;
  let i=0;
  const t=setInterval(()=>{
    if(i<text.length){el.textContent+=text[i++];la.scrollTop=la.scrollHeight;}
    else{clearInterval(t);el.classList.remove('cur');setTimeout(nextLog,20);}
  },6);
}

function getLogText(){
  return [...document.querySelectorAll('#log .le')].map(el=>el.textContent).join('\n');
}
function clearLog(){
  document.getElementById('log').innerHTML='';
  lq=[]; typing=false;
  appendLog('SYS: Log cleared.');
}
function copyLog(){
  const text=getLogText();
  if(!text.trim()){appendLog('SYS: Log empty.');return;}
  if(window.pywebview && window.pywebview.api.copy_text){
    window.pywebview.api.copy_text(text).then(ok=>{
      appendLog(ok?'SYS: Log copied to clipboard.':'ERR: Clipboard copy failed.');
    });
  } else if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text)
      .then(()=>appendLog('SYS: Log copied to clipboard.'))
      .catch(()=>appendLog('ERR: Clipboard copy failed.'));
  } else {
    appendLog('ERR: Clipboard unavailable.');
  }
}
function focusInput(){
  const inp=document.getElementById('ci');
  if(inp) inp.focus();
}

// ── File ────────────────────────────────────────────────────────────────
const ICONS={image:'🖼',video:'🎬',audio:'🎵',pdf:'📄',word:'📝',
  excel:'📊',code:'💻',archive:'📦',text:'📃',data:'🔧',unknown:'📎'};
const EXTS={jpg:'image',jpeg:'image',png:'image',gif:'image',webp:'image',bmp:'image',
  mp4:'video',avi:'video',mov:'video',mkv:'video',wmv:'video',webm:'video',
  mp3:'audio',wav:'audio',ogg:'audio',flac:'audio',m4a:'audio',aac:'audio',
  pdf:'pdf',doc:'word',docx:'word',xls:'excel',xlsx:'excel',
  py:'code',js:'code',ts:'code',html:'code',css:'code',java:'code',cpp:'code',go:'code',
  zip:'archive',rar:'archive',tar:'archive',gz:'archive',
  txt:'text',md:'text',json:'data',csv:'data',xml:'data'};

function dzClick(){
  if(window.pywebview)
    window.pywebview.api.open_file_dialog().then(p=>{if(p)onFile(p);});
}
function dzDragOver(e){e.preventDefault();document.getElementById('dz').classList.add('drag-over');}
function dzDragLeave(){document.getElementById('dz').classList.remove('drag-over');}
function dzDrop(e){e.preventDefault();dzDragLeave();dzClick();}

function onFile(path){
  const parts=path.replace(/\\/g,'/').split('/');
  const name=parts[parts.length-1];
  const ext=(name.split('.').pop()||'').toLowerCase();
  const cat=EXTS[ext]||'unknown';
  document.getElementById('f-icon').textContent=ICONS[cat]||'📎';
  document.getElementById('f-name').textContent=name;
  document.getElementById('f-ext').textContent=ext.toUpperCase();
  document.getElementById('dz-idle').style.display='none';
  document.getElementById('dz-file').style.display='flex';
  document.getElementById('dz').classList.add('has-file');
  document.getElementById('fhint').textContent='✓ '+name+' loaded — tell ARIS what to do';
  appendLog('FILE: '+name+' loaded');
}
function clearFile(e){
  if(e) e.stopPropagation();
  document.getElementById('dz-idle').style.display='flex';
  document.getElementById('dz-file').style.display='none';
  document.getElementById('dz').classList.remove('has-file');
  document.getElementById('fhint').textContent='No file loaded — click above to upload';
  if(window.pywebview) window.pywebview.api.clear_file();
}

// ── Commands ────────────────────────────────────────────────────────────
function sendCmd(){
  const inp=document.getElementById('ci');
  const t=inp.value.trim(); if(!t) return;
  inp.value=''; appendLog('You: '+t);
  if(window.pywebview) window.pywebview.api.send_command(t);
}
document.getElementById('ci').addEventListener('keydown',e=>{if(e.key==='Enter')sendCmd();});

function toggleMute(){
  if(window.pywebview)
    window.pywebview.api.toggle_mute().then(m=>updateState(m?'MUTED':'LISTENING'));
}
function toggleFS(){
  document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();
}
document.addEventListener('keydown',e=>{
  if(e.key==='F4'){e.preventDefault();toggleMute();}
  if(e.key==='F11'){e.preventDefault();toggleFS();}
  if(e.ctrlKey && e.shiftKey && e.key.toLowerCase()==='l'){e.preventDefault();clearLog();}
  if(e.ctrlKey && e.shiftKey && e.key.toLowerCase()==='k'){e.preventDefault();focusInput();}
});

// ── Setup ────────────────────────────────────────────────────────────────
let currentOS='windows';
function selOS_(os){
  currentOS=os;
  ['windows','mac','linux'].forEach(o=>{
    const b=document.getElementById('os-'+o[0]);
    b.className='osbtn'+(o===os?' '+{windows:'win',mac:'mac',linux:'lin'}[o]:'');
  });
}
function submitSetup(){
  const gk=document.getElementById('gkey').value.trim();
  const ok=document.getElementById('orkey').value.trim();
  document.getElementById('gkey').classList.toggle('err',!gk);
  document.getElementById('orkey').classList.toggle('err',!ok);
  if(!gk||!ok) return;
  if(window.pywebview)
    window.pywebview.api.save_config(gk,ok,currentOS).then(s=>{
      if(s){document.getElementById('ovl').classList.add('hide');
            appendLog('SYS: Initialised. ARIS online.');updateState('LISTENING');}
    });
}

// ── Init ────────────────────────────────────────────────────────────────
function initApp(){
  if(!window.pywebview) return;
  window.pywebview.api.on_loaded().then(ok=>{
    if(!ok){
      document.getElementById('ovl').classList.remove('hide');
      window.pywebview.api.get_os().then(os=>{
        const names={windows:'Windows',mac:'macOS',linux:'Linux'};
        document.getElementById('os-det').textContent='Auto-detected: '+(names[os]||os);
        selOS_(os);
      });
    }
  });
  setInterval(()=>window.pywebview.api.get_metrics().then(updateMetrics),2000);
}

window.addEventListener('pywebviewready',initApp);
window.addEventListener('load',()=>setTimeout(initApp,600));
</script>
</body>
</html>
"""



# ── JavaScript bridge (exposed to the webview) ────────────────────────────────

class ArisAPI:
    """Methods on this class are callable from JavaScript via window.pywebview.api.*"""

    def __init__(self):
        self._window: "webview.Window | None" = None
        self._ready           = False
        self._muted           = False
        self._current_file: "str | None" = None
        self.on_text_command  = None

    # ── called by JS ─────────────────────────────────────────────────────────

    def on_loaded(self) -> bool:
        """JS calls this when the page is ready; returns True if already configured."""
        configured = self._check_config()
        if configured:
            self._ready = True
        return configured

    def get_os(self) -> str:
        return {"Darwin": "mac", "Windows": "windows"}.get(_OS, "linux")

    def get_metrics(self) -> dict:
        snap = _metrics.snapshot()
        try:
            elapsed = time.time() - psutil.boot_time()
            h, m = int(elapsed // 3600), int((elapsed % 3600) // 60)
            snap["uptime"] = f"UP  {h:02d}:{m:02d}"
        except Exception:
            snap["uptime"] = "UP  --:--"
        try:
            snap["proc_count"] = len(psutil.pids())
        except Exception:
            snap["proc_count"] = 0
        snap["os_name"] = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        return snap

    def send_command(self, text: str):
        if text and self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(text,), daemon=True).start()

    def copy_text(self, text: str) -> bool:
        try:
            pyperclip.copy(text or "")
            return True
        except Exception:
            return False

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    def open_file_dialog(self) -> "str | None":
        if not self._window:
            return None
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=(
                "All Files (*.*)",
                "Images (*.jpg;*.jpeg;*.png;*.gif;*.webp;*.bmp)",
                "Documents (*.pdf;*.docx;*.txt;*.md;*.pptx)",
                "Data (*.csv;*.xlsx;*.json;*.xml)",
                "Code (*.py;*.js;*.ts;*.html;*.css;*.java;*.cpp;*.go)",
                "Audio (*.mp3;*.wav;*.ogg;*.m4a;*.aac;*.flac)",
                "Video (*.mp4;*.avi;*.mov;*.mkv;*.wmv;*.webm)",
                "Archives (*.zip;*.rar;*.tar;*.gz;*.7z)",
            ),
        )
        if result:
            self._current_file = result[0]
            return result[0]
        return None

    def clear_file(self):
        self._current_file = None
        return True

    def save_config(self, gemini_key: str, openrouter_key: str, os_name: str) -> bool:
        if not gemini_key or not openrouter_key:
            return False
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({
                "gemini_api_key":     gemini_key,
                "openrouter_api_key": openrouter_key,
                "os_system":          os_name,
            }, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        return True

    # ── internal ─────────────────────────────────────────────────────────────

    def _check_config(self) -> bool:
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return (bool(d.get("gemini_api_key")) and
                    bool(d.get("openrouter_api_key")) and
                    bool(d.get("os_system")))
        except Exception:
            return False


# ── Root shim (mirrors tkinter's root.mainloop / root.protocol) ───────────────

class _RootShim:
    def mainloop(self):
        webview.start(debug=False)

    def protocol(self, *_):
        pass


# ── Public UI class ───────────────────────────────────────────────────────────

class ArisUI:
    """
    Drop-in replacement for the old JarvisUI.
    Uses pywebview + HTML/CSS/JS for a professional animated interface.
    """

    def __init__(self, face_path: str, size=None):
        self._api = ArisAPI()

        self._win = webview.create_window(
            title="A.R.I.S — Adaptive Response Intelligence System",
            html=_HTML,
            js_api=self._api,
            width=1100,
            height=720,
            min_size=(820, 580),
            background_color="#00060a",
            text_select=False,
        )
        self._api._window = self._win
        self.root = _RootShim()

    # ── properties ───────────────────────────────────────────────────────────

    @property
    def muted(self) -> bool:
        return self._api._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._api._muted:
            self._api._muted = v
            self.set_state("MUTED" if v else "LISTENING")

    @property
    def current_file(self) -> "str | None":
        return self._api._current_file

    @property
    def on_text_command(self):
        return self._api.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._api.on_text_command = cb

    # ── methods ───────────────────────────────────────────────────────────────

    def set_state(self, state: str):
        self._eval(f'updateState("{state}")')

    def write_log(self, text: str):
        safe = (text.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", ""))
        self._eval(f'appendLog("{safe}")')

    def wait_for_api_key(self):
        while not self._api._ready:
            time.sleep(0.1)

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

    # ── internal ─────────────────────────────────────────────────────────────

    def _eval(self, js: str):
        try:
            self._win.evaluate_js(js)
        except Exception:
            pass


# Backward-compatibility alias
JarvisUI = ArisUI
