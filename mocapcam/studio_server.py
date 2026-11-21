"""MOCAM Studio : pilotage des téléphones depuis le PC.

  python -m mocapcam studio            (ou double-clic sur « MOCAM Studio.bat »)

- les téléphones (app MOCAM) se connectent en scannant un QR code (Wi-Fi ou partage de
  connexion d'un téléphone) ;
- aperçu en direct, état (batterie, i/s, synchro d'horloge), champ de vision ;
- vérification des boules au sol en direct ;
- enregistrement lancé en même temps sur tous les téléphones + signal sonore de synchro ;
- rapatriement automatique des vidéos, traitement des prises (file d'attente) ;
- taille de l'acteur en cm ; personnages riggés animés avec les prises (aperçu 3D).
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import wave
import webbrowser
import zipfile
from dataclasses import dataclass, field

import numpy as np
from aiohttp import WSMsgType, web

from . import fbx, markers as mk, pipeline, sync

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio_ui")
# version du serveur : l'interface (index.html) vérifie qu'elle parle au serveur de la même version
STUDIO_VERSION = "1.8.0"

# horloge du PC en ms (monotone, calée sur l'heure système au démarrage)
_EPOCH = time.time() * 1000 - time.monotonic() * 1000


def now_ms():
    return _EPOCH + time.monotonic() * 1000


def safe_name(s, default="x"):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", s).strip("_")
    return s[:40] or default


# cartes réseau virtuelles : jamais joignables par les téléphones
VIRTUAL_ADAPTERS = ("virtualbox", "vmware", "vethernet", "hyper-v", "wsl", "loopback", "bluetooth", "vpn",
                    "tailscale", "zerotier", "hamachi", "docker", "npcap", "teredo", "isatap", "tap-", "wintun",
                    "nordlynx", "radmin")
WIFI_WORDS = ("wi-fi", "wifi", "wlan", "wireless", "sans fil")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    enc = "oem" if sys.platform.startswith("win") else "utf-8"
    try:
        out = r.stdout.decode(enc, "replace")
    except LookupError:
        out = r.stdout.decode("utf-8", "replace")
    return r.returncode, out


def windows_adapters():
    """[(carte réseau, description, adresse IPv4)] d'après ipconfig /all (Windows)."""
    rc, out = _run(["ipconfig", "/all"])
    res, adapter, desc = [], "", ""
    for line in out.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            adapter, desc = line.strip().rstrip(":").strip(" \xa0\ufffd"), ""
        elif "Description" in line and ":" in line:
            desc = line.split(":", 1)[1].strip()
        elif "IPv4" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                res.append((adapter, desc, m.group(1)))
    return res


def windows_hotspot():
    """Point d'accès mobile de Windows (le PC partage sa connexion) : {on, ssid}, ou None si inconnu."""
    ps = ("$p=[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,"
          "ContentType=WindowsRuntime]::GetInternetConnectionProfile();"
          "$m=[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,"
          "Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($p);"
          "Write-Output $m.TetheringOperationalState.ToString();"
          "Write-Output $m.GetCurrentAccessPointConfiguration().Ssid")
    rc, out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=10)
    lines = [x.strip() for x in out.strip().splitlines() if x.strip()]
    if rc != 0 or not lines or lines[0] not in ("On", "Off", "InTransition", "Unknown"):
        return None
    return dict(on=lines[0] == "On", ssid=lines[1] if len(lines) > 1 else None)


def _adapter_kind(name, desc):
    t = f"{name} {desc}".lower()
    if "wi-fi direct" in t or "hosted network" in t:
        return "hotspot"                       # carte du point d'accès mobile de Windows
    if any(v in t for v in VIRTUAL_ADAPTERS):
        return "virtual"
    return "wifi" if any(w in t for w in WIFI_WORDS) else "wired"


def _adapter_label(kind, name):
    if kind == "hotspot":
        return "Point d'accès mobile du PC"
    n = re.sub(r"^(Carte (Ethernet|réseau sans fil|r.seau sans fil)|Ethernet adapter|Wireless LAN adapter)\s+",
               "", name, flags=re.I)
    return n or name


def network_info(port=8765):
    """Adresses du PC (Wi-Fi d'abord, cartes virtuelles exclues), nom du Wi-Fi, point d'accès mobile
    du PC, profil réseau Windows (public = pare-feu plus strict) et règle de pare-feu MOCAM."""
    info = dict(hosts=[], ssid=None, public=False, firewall_rule=None, adapters=[], hotspot=None)
    if sys.platform.startswith("win"):
        ad = [x for x in windows_adapters() if not x[2].startswith(("127.", "169.254."))]
        hs = windows_hotspot()
        info["hotspot"] = hs
        real = []
        for name, desc, ip in ad:
            k = _adapter_kind(name, desc)
            if k == "virtual" or (k == "hotspot" and hs is not None and not hs["on"]):
                continue                       # VirtualBox, VPN… ou point d'accès éteint
            real.append((k, name, ip))
        real.sort(key=lambda x: {"wifi": 0, "hotspot": 1, "wired": 2}[x[0]])
        info["adapters"] = [dict(name=_adapter_label(k, n), ip=ip, kind=k) for k, n, ip in real]
        info["hosts"] = [ip for _, _, ip in real] or [x[2] for x in ad]
        rc, out = _run(["netsh", "wlan", "show", "interfaces"])
        m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.M)
        if m:
            info["ssid"] = m.group(1)
        rc, out = _run(["powershell", "-NoProfile", "-Command",
                        "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory | ConvertTo-Json"])
        try:
            prof = json.loads(out) if out.strip() else []
            prof = prof if isinstance(prof, list) else [prof]
            info["public"] = any(str(p.get("NetworkCategory")) in ("0", "Public") for p in prof)
        except ValueError:
            pass
        rc, _ = _run(["netsh", "advfirewall", "firewall", "show", "rule", "name=MOCAM Studio"])
        info["firewall_rule"] = rc == 0
    if not info["hosts"]:
        info["hosts"] = lan_addresses()
    if not info["adapters"]:
        info["adapters"] = [dict(name="réseau", ip=ip) for ip in info["hosts"] if not ip.startswith("127.")]
    return info


def allow_firewall(port):
    """Ouvre le port de MOCAM Studio sur tous les réseaux (public compris : partage de
    connexion d'un téléphone). Windows demande l'accord de l'administrateur."""
    args = (f'advfirewall firewall add rule name="MOCAM Studio" dir=in action=allow protocol=TCP '
            f'localport={port} profile=any')
    ps = f"Start-Process netsh -ArgumentList '{args}' -Verb RunAs -WindowStyle Hidden -Wait"
    rc, out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=120)
    return rc == 0


def lan_addresses():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in ips:
        if ip not in out and not ip.startswith(("127.", "169.254.")):
            out.append(ip)
    return out or ["127.0.0.1"]


@dataclass
class Cam:
    id: str
    ws: web.WebSocketResponse
    name: str
    platform: str = ""
    model: str = ""
    status: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)     # (rtt, offset)
    offset: float = 0.0                             # horloge téléphone - horloge PC (ms)
    rtt: float = float("nan")
    preview: bytes = b""
    preview_ts: float = 0.0
    check: dict = None
    check_jpg: bytes = b""
    check_seq: int = 0
    take: dict = field(default_factory=dict)        # état pour la prise en cours
    locked: bool = False
    app: str = ""
    connected_at: float = field(default_factory=now_ms)

    def recording_flag(self):
        return bool(self.status.get("recording"))

    def public(self):
        st = self.status
        return dict(
            id=self.id, name=self.name, platform=self.platform, model=self.model,
            battery=st.get("battery"), storage_free=st.get("storage_free"), fps=st.get("fps"),
            stabilization=st.get("stabilization"), exposure=st.get("exposure"),
            recording=st.get("recording", False), locked=st.get("locked", self.locked),
            snapshot_size=st.get("snapshot_size"), device=st.get("device"),
            device_type=st.get("device_type"), resolution=st.get("resolution"),
            res720=st.get("res720"), supports60=st.get("supports60"),
            lens=st.get("lens"), ultra_available=st.get("ultra_available"), zoom=st.get("zoom"),
            fov=st.get("fov"), fov_options=st.get("fov_options"), facing=st.get("facing") or "back", app=self.status.get("app") or self.app,
            sync_ms=None if np.isnan(self.rtt) else round(self.rtt / 2, 1),
            preview_ts=self.preview_ts, check=self.check, take=self.take,
        )


class Studio:
    def __init__(self, studio_dir, captures_dir, port):
        self.studio_dir = os.path.abspath(studio_dir)
        self.captures_dir = os.path.abspath(captures_dir)
        os.makedirs(self.captures_dir, exist_ok=True)
        # prises de visage (capture faciale, une caméra) : dossier « visages » à côté des prises
        self.faces_dir = os.path.join(os.path.dirname(self.captures_dir), "visages")
        os.makedirs(self.faces_dir, exist_ok=True)
        if not os.path.exists(os.path.join(self.studio_dir, "studio.json")):
            pipeline.init_studio(self.studio_dir)
        self.port = port
        self.token = secrets.token_hex(4)
        self.cams: dict[str, Cam] = {}
        self.uis: set = set()
        self.take = None            # prise en cours
        self.proc = None            # traitement en cours
        self.proc_log = []
        self._n = 0
        self._dirty = asyncio.Event()
        self.chirp_wav = None
        self.prefer60 = True        # priorité à 60 i/s (720p si le téléphone l'exige)
        self.fov_by_name = self._load_fov()     # champ de vision choisi par caméra (×0,5 … ×1)
        self.preview_fps = 3.0
        self.face_live = None          # caméra suivie en direct dans l'onglet Visage
        self.face_live_busy = False
        self.face_tracker = None
        self.face_pool = None
        self.net = dict(hosts=lan_addresses(), ssid=None, public=False, firewall_rule=None, adapters=[])
        self.queue = []             # traitements en attente
        self.char_busy = None       # import de personnage en cours
        self.has_blender = bool(fbx.find_blender(self.cfg().get("blender") or None))

    def _fov_path(self):
        return os.path.join(self.studio_dir, "objectifs.json")

    def _load_fov(self):
        try:
            with open(self._fov_path(), encoding="utf-8") as f:
                d = dict(json.load(f))
        except (OSError, ValueError):
            return {}
        out = {}
        for k, v in d.items():      # ancien format : "wide" / "ultra"
            out[k] = 0.5 if v == "ultra" else 1.0 if v == "wide" else float(v)
        return out

    def _save_fov(self):
        try:
            with open(self._fov_path(), "w", encoding="utf-8") as f:
                json.dump(self.fov_by_name, f, indent=2)
        except OSError:
            pass

    def cfg(self):
        return pipeline.load_studio(self.studio_dir)

    def save_cfg_value(self, key, value):
        path = os.path.join(self.studio_dir, "studio.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg[key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    async def install_bundled(self):
        """Personnages fournis (personnages_fournis/, ex. Y Bot) installés au démarrage."""
        try:
            done = await asyncio.get_running_loop().run_in_executor(
                None, lambda: pipeline.install_bundled_characters(
                    self.studio_dir, self.cfg().get("blender") or None, lambda x: print("  " + str(x).strip())))
            if done:
                self.changed()
        except Exception as e:  # noqa: BLE001
            print(f"  personnages fournis : {e}")

    async def net_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                info = await loop.run_in_executor(None, network_info, self.port)
                hb = bool(fbx.find_blender(self.cfg().get("blender") or None))
                if info != self.net or hb != self.has_blender:
                    self.net, self.has_blender = info, hb
                    self.changed()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5)

    # ------------------------------------------------------------------ état -> interface
    def takes_list(self):
        out = []
        for d in sorted(os.listdir(self.captures_dir), reverse=True):
            p = os.path.join(self.captures_dir, d)
            if not os.path.isdir(p) or d.startswith("_"):
                continue
            vids = [f for f in os.listdir(p) if f.lower().endswith((".mp4", ".mov"))]
            fbx_file = os.path.join(p, f"{d}.fbx")
            res = next((r for r in (fbx_file, os.path.join(p, "mocapcam", f"{d}.mocap.json"),
                                    os.path.join(p, f"{d}.mocap.json")) if os.path.exists(r)), fbx_file)
            meta = {}
            if os.path.exists(os.path.join(p, "take.json")):
                try:
                    with open(os.path.join(p, "take.json"), encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    pass
            height = meta.get("actor_height_cm")
            rp = os.path.join(p, "mocapcam", "rapport.json")
            measured = None
            if os.path.exists(rp):
                try:
                    with open(rp, encoding="utf-8") as f:
                        h = json.load(f).get("actor_height_m")
                    measured = round(h * 100) if h else None
                except (OSError, ValueError):
                    pass
            # personnages animés : FBX et aperçu GLB écrits (le GLB arrive juste après le FBX)
            chars = [c for c in os.listdir(p) if c.startswith(d + "_") and c.endswith(".fbx")
                     and os.path.exists(pipeline.character_outputs(p, c[len(d) + 1:-4])[1])]
            out.append(dict(name=d, videos=vids, processed=os.path.exists(res),
                            result=res if os.path.exists(res) else None,
                            fbx=os.path.exists(fbx_file), height_cm=height, measured_cm=measured,
                            characters=[c[len(d) + 1:-4] for c in chars],
                            preview=os.path.exists(os.path.join(p, "mocapcam", f"{d}.mocap.json")),
                            created=meta.get("created"), duration=meta.get("duration_s")))
        return out[:60]

    def faces_list(self):
        from . import face as facemod
        out = []
        if not os.path.isdir(self.faces_dir):
            return out
        for d in sorted(os.listdir(self.faces_dir), reverse=True):
            p = os.path.join(self.faces_dir, d)
            if not os.path.isdir(p) or d.startswith((".", "_")):
                continue
            meta = facemod.read_meta(p)
            vid = facemod.find_video(p)
            o = facemod.outputs(p)
            out.append(dict(name=d, video=os.path.basename(vid) if vid else None, created=meta.get("created"),
                            duration=meta.get("duration_s"), camera=meta.get("camera"),
                            processed=os.path.exists(facemod.curves_path(p)), fbx=os.path.exists(o["fbx"]),
                            glb=os.path.exists(o["glb"]), frames=meta.get("frames"), fps=meta.get("fps"),
                            view=os.path.getmtime(o["view"]) if os.path.exists(o["view"]) else 0,
                            lissage=meta.get("lissage", "moyen"), intensite=meta.get("intensite", 100)))
        return out[:80]

    def next_face_name(self):
        n = 1
        names = set(os.listdir(self.faces_dir)) if os.path.isdir(self.faces_dir) else set()
        while f"visage_{n:03d}" in names:
            n += 1
        return f"visage_{n:03d}"

    def next_take_name(self):
        n = 1
        names = {t["name"] for t in self.takes_list()}
        while f"prise_{n:03d}" in names:
            n += 1
        return f"prise_{n:03d}"

    def state(self):
        cfg = self.cfg()
        proc = None
        if self.proc is not None:
            proc = {k: self.proc.get(k) for k in ("take", "running", "ok", "kind", "label", "step", "steps", "step_label")}
        q = self.qr_payload()
        return dict(
            type="state", cams=[c.public() for c in self.cams.values()],
            take=self.take, takes=self.takes_list(), next_take=self.next_take_name(),
            processing=proc, queue=[dict(kind=j["kind"], take=j["take"], label=j["label"]) for j in self.queue],
            qr=q, qr_key=hashlib.sha1(json.dumps(q, sort_keys=True).encode()).hexdigest()[:12],
            net=dict(ssid=self.net.get("ssid"), public=self.net.get("public"),
                     firewall_rule=self.net.get("firewall_rule"), adapters=self.net.get("adapters", []),
                     hotspot=self.net.get("hotspot")),
            now=now_ms(), studio=self.studio_dir, captures=self.captures_dir,
            prefer60=self.prefer60, preview_fps=self.preview_fps,
            actor_height_cm=cfg.get("actor_height_cm"),
            characters=pipeline.list_characters(self.studio_dir),
            character=pipeline.default_character(self.studio_dir, cfg),
            faces=self.faces_list(),
            next_face=self.next_face_name(),
            char_busy=self.char_busy, blender=self.has_blender, version=STUDIO_VERSION,
        )

    def qr_payload(self):
        return dict(mocapcam=1, hosts=self.net.get("hosts") or lan_addresses(), port=self.port, token=self.token)

    def changed(self):
        self._dirty.set()

    async def broadcast(self, msg):
        data = json.dumps(msg)
        for ws in list(self.uis):
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError):
                self.uis.discard(ws)

    async def state_pusher(self):
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self.broadcast(self.state())
            await asyncio.sleep(0.2)

    async def toast(self, msg, level="info"):
        await self.broadcast(dict(type="toast", msg=msg, level=level))

    # ------------------------------------------------------------------ téléphones
    async def send(self, cam, msg):
        try:
            await cam.ws.send_str(json.dumps(msg))
        except (ConnectionError, RuntimeError):
            pass

    async def ping_loop(self, cam):
        n = 0
        while not cam.ws.closed:
            await self.send(cam, dict(t="ping", s0=now_ms()))
            n += 1
            await asyncio.sleep(0.1 if n < 15 else 1.0)

    def on_pong(self, cam, msg):
        s1 = now_ms()
        s0 = float(msg["s0"])
        rtt = s1 - s0
        off = float(msg["c"]) - (s0 + s1) / 2
        cam.samples.append((rtt, off, s1))
        cam.samples = [x for x in cam.samples if s1 - x[2] < 60000][-60:]
        best = min(cam.samples, key=lambda x: x[0])
        cam.rtt, cam.offset = best[0], best[1]

    def unique_name(self, name, cam_id=None):
        base = safe_name(name, "camera").lower()
        taken = {c.name for c in self.cams.values() if c.id != cam_id}
        n, out = 2, base
        while out in taken:
            out = f"{base}_{n}"
            n += 1
        return out

    async def handle_phone(self, request):
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        cam = None
        pinger = None
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        m = json.loads(msg.data)
                    except ValueError:
                        continue
                    t = m.get("t")
                    if cam is None:
                        if t != "hello":
                            continue
                        if m.get("token") != self.token:
                            await ws.send_str(json.dumps(dict(t="error", message="QR code périmé : rescannez-le")))
                            await ws.close()
                            break
                        self._n += 1
                        cid = f"c{self._n}"
                        cam = Cam(id=cid, ws=ws, name=self.unique_name(m.get("name", "camera")),
                                  platform=m.get("platform", ""), model=m.get("model", ""),
                                  app=str(m.get("app", "")))
                        self.cams[cid] = cam
                        fov = self.fov_by_name.get(cam.name)
                        await self.send(cam, dict(t="welcome", id=cid, name=cam.name,
                                                  prefer60=self.prefer60, preview=self.preview_fps,
                                                  lens="ultra" if fov is not None and fov < 0.99 else "wide",
                                                  **({"fov": fov} if fov is not None else {})))
                        pinger = asyncio.create_task(self.ping_loop(cam))
                        await self.toast(f"Caméra connectée : {cam.name}")
                        self.changed()
                        continue
                    await self.on_phone_message(cam, m)
                elif msg.type == WSMsgType.BINARY and cam is not None:
                    await self.on_phone_binary(cam, msg.data)
        finally:
            if pinger:
                pinger.cancel()
            if cam is not None:
                self.snapshot_cam(cam)
                self.cams.pop(cam.id, None)
                await self.check_take_complete()
                await self.toast(f"Caméra déconnectée : {cam.name}", "warn")
                self.changed()
        return ws

    async def on_phone_message(self, cam, m):
        t = m.get("t")
        if t == "pong":
            self.on_pong(cam, m)
            return
        if t == "status":
            old_fov = cam.status.get("fov")
            cam.status = m
            fov = m.get("fov")
            if fov is not None and fov != old_fov and not cam.recording_flag():
                # champ de vision changé sur le téléphone : retenu, et la vérification est à refaire
                if self.fov_by_name.get(cam.name) != fov:
                    self.fov_by_name[cam.name] = fov
                    self._save_fov()
                if old_fov is not None:
                    cam.check = None
            self.changed()
        elif t == "recording":
            cam.take.update(state="recording", called_at_pc=m["called_at"] - cam.offset,
                            started_at_pc=m["started_at"] - cam.offset)
            self.snapshot_cam(cam)
            await self.maybe_chirp()
            self.changed()
        elif t == "recorded":
            cam.take.update(state="uploading", duration=m.get("duration"), size=m.get("size"))
            self.changed()
        elif t == "upload":
            cam.take.update(state="uploading", sent=m.get("sent"), total=m.get("total"))
            self.changed()
        elif t == "uploaded":
            cam.take["state"] = "done"
            self.snapshot_cam(cam)
            await self.check_take_complete()
            self.changed()
        elif t in ("upload_error", "error"):
            if t == "upload_error":
                cam.take["state"] = "error"
            await self.toast(f"{cam.name} : {m.get('message')}", "error")
            self.changed()
        elif t == "face_record":          # bouton REC de l'app (mode Visage)
            busy = self.take and self.take["state"] in ("starting", "recording", "stopping")
            if busy and self.take.get("kind") == "visage" and cam.id in self.take.get("cams", []):
                await self.stop_take()
            elif busy:
                await self.send(cam, dict(t="error", message="un enregistrement est déjà en cours"))
                await self.toast(f"{cam.name} : un enregistrement est déjà en cours", "warn")
            else:
                await self.start_take("", cam_id=cam.id, kind="visage")
        elif t == "face_stop":
            if self.take and self.take.get("kind") == "visage" and cam.id in self.take.get("cams", []):
                await self.stop_take()
        elif t == "locked":
            cam.locked = bool(m.get("ok"))
            if m.get("message"):
                await self.toast(f"{cam.name} : {m['message']}", "warn")
            self.changed()

    async def on_phone_binary(self, cam, data):
        if not data:
            return
        kind, jpg = data[0], bytes(data[1:])
        if kind == 1:
            cam.preview, cam.preview_ts = jpg, now_ms()
            await self.broadcast(dict(type="preview", cam=cam.id, ts=cam.preview_ts))
            if cam.id == self.face_live and not self.face_live_busy:
                asyncio.ensure_future(self.track_live(cam, jpg))
        elif kind == 2:
            seq = cam.check_seq
            cam.check = dict(pending=True, analysing=True, tries=(cam.check or {}).get("tries", 1))
            self.changed()
            loop = asyncio.get_running_loop()
            res, dbg = await loop.run_in_executor(None, self.check_markers, cam.name, jpg,
                                                  cam.status.get("lens") or "wide", cam.status.get("fov"))
            if seq == cam.check_seq:
                cam.check, cam.check_jpg = res, dbg
                self.changed()

    # ------------------------------------------------------------------ boules au sol
    async def request_check(self, cam, tries=3, wait_s=6.0):
        """Demande l'image pleine résolution et la redemande si elle n'arrive pas (téléphone
        occupé à envoyer l'aperçu, réseau lent) : un seul clic suffit pour toutes les caméras."""
        cam.check_seq += 1
        seq = cam.check_seq
        for k in range(1, tries + 1):
            if cam.ws.closed or seq != cam.check_seq:
                return
            cam.check = dict(pending=True, tries=k)
            self.changed()
            await self.send(cam, dict(t="snapshot"))
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait_s:
                await asyncio.sleep(0.2)
                if seq != cam.check_seq or cam.ws.closed:
                    return
                if cam.check and not cam.check.get("pending"):
                    return
                if cam.check and cam.check.get("analysing"):
                    t0 = time.monotonic()           # image reçue : analyse en cours
        if seq == cam.check_seq and cam.check and cam.check.get("pending") and not cam.check.get("analysing"):
            cam.check = dict(ok=False, message="pas de réponse du téléphone : réessayez")
            self.changed()

    def check_markers(self, name, jpg, lens="wide", fov=None):
        import cv2
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return dict(ok=False, message="image illisible"), b""
        d = os.path.join(self.captures_dir, "_controle")
        os.makedirs(d, exist_ok=True)
        cv2.imwrite(os.path.join(d, f"{name}.jpg"), img)
        try:
            cfg = pipeline.load_studio(self.studio_dir)
            mlist = [dict(m, position=m["position_3d"]) for m in cfg["markers"]]
            balls = [dict(name=m["name"], color=m.get("color"), p=list(m["position"][:2])) for m in cfg["markers"]]
            view = dict(name=name, bg=img, K=None, dist=None, lens=lens, fov=fov,
                        cands=mk.find_candidates(img, mlist))
            # une seule image : on accepte le miroir, mais la recherche de boules échangées
            # (moins sûre avec une seule vue) est faite au traitement, sur toutes les caméras
            ml, poses, layout = pipeline.choose_layout([view], mlist, log=lambda m: None, swaps=False)
            pose = poses[0]
            if pose is None:
                raise RuntimeError("moins de 4 boules reconnues, ou disposition différente de studio.json")
            vol = mk.volume_box(ml, cfg["volume"]["margin"], cfg["volume"]["height"])
            dbg = mk.draw_debug(img, ml, pose["K"], pose["dist"], pose, vol)
            res = dict(ok=True, rms=round(pose["rms"], 2), seen=len(pose["used"]), total=len(mlist), balls=balls,
                       missing=pose["missing"], focal=round(float(pose["K"][0, 0]), 0),
                       center=[round(v, 2) for v in pose["center"]], size=[img.shape[1], img.shape[0]],
                       aligned=bool(pose.get("aligned")),
                       note=None if pose["rms"] <= 8 else "écart élevé : disposition différente de studio.json ? "
                                                          "(couleurs échangées, distances)")
        except Exception as e:  # noqa: BLE001
            dbg = img.copy()
            cv2.putText(dbg, "Boules non trouvees", (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 255), 3)
            try:
                balls = [dict(name=m["name"], color=m.get("color"), p=list(m["position"][:2]))
                         for m in pipeline.load_studio(self.studio_dir)["markers"]]
            except Exception:  # noqa: BLE001
                balls = []
            res = dict(ok=False, message=str(e), size=[img.shape[1], img.shape[0]], balls=balls,
                       missing=[b["name"] for b in balls])
        ok, enc = cv2.imencode(".jpg", dbg, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return res, enc.tobytes() if ok else b""

    # ------------------------------------------------------------------ disposition des boules
    STANDARD = {"A": (0, 0), "B": (1, 0), "C": (1, 1), "D": (0, 1), "E": (0, 0.5)}

    def layout_info(self):
        """Côté du carré décrit dans studio.json (disposition standard A-B-C-D + E au milieu)."""
        try:
            with open(os.path.join(self.studio_dir, "studio.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            pos = {m["name"]: m["position"] for m in cfg.get("markers", [])}
            if set(pos) != set(self.STANDARD):
                return dict(standard=False)
            side = float(np.hypot(pos["B"][0] - pos["A"][0], pos["B"][1] - pos["A"][1]))
            ok = all(np.hypot(pos[n][0] - u * side, pos[n][1] - v * side) < 1e-3
                     for n, (u, v) in self.STANDARD.items())
            return dict(standard=ok, side=round(side, 3))
        except (OSError, ValueError, KeyError, TypeError):
            return dict(standard=False)

    def set_square_side(self, side):
        path = os.path.join(self.studio_dir, "studio.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        names = {m["name"] for m in cfg.get("markers", [])}
        if names != set(self.STANDARD):
            raise ValueError("studio.json décrit une disposition personnalisée : modifiez-le à la main")
        for m in cfg["markers"]:
            u, v = self.STANDARD[m["name"]]
            z = m["position"][2] if len(m["position"]) > 2 else 0.0
            m["position"] = [round(u * side, 4), round(v * side, 4), z]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------ enregistrement
    async def start_take(self, name, cam_id=None, kind="corps"):
        if self.take and self.take["state"] in ("starting", "recording", "stopping"):
            await self.toast("Un enregistrement est déjà en cours", "warn")
            return
        cams = list(self.cams.values())
        if cam_id is not None:
            cams = [c for c in cams if c.id == cam_id]
        if not cams:
            await self.toast("Aucune caméra connectée", "error")
            return
        if kind == "visage":
            name = safe_name(name, self.next_face_name())
            d = os.path.join(self.faces_dir, name)
        else:
            name = safe_name(name, self.next_take_name())
            d = os.path.join(self.captures_dir, name)
        if os.path.exists(d) and any(f.lower().endswith((".mp4", ".mov")) for f in os.listdir(d)):
            await self.toast(f"La prise {name} existe déjà : choisissez un autre nom", "error")
            return
        os.makedirs(d, exist_ok=True)
        self.take = dict(name=name, state="starting", dir=d, cams=[c.id for c in cams], kind=kind,
                         created=time.strftime("%Y-%m-%d %H:%M:%S"), chirp_at_pc=None,
                         actor_height_cm=self.cfg().get("actor_height_cm"))
        for c in cams:
            c.take = dict(name=name, state="armed")
            await self.send(c, dict(t="arm", take=name))
        await asyncio.sleep(0.6)
        at = now_ms() + 1500
        self.take["at_pc"] = at
        for c in cams:
            await self.send(c, dict(t="start", take=name, at=at + c.offset))
        self.changed()
        if kind == "visage":          # une seule caméra : pas de bip de synchronisation
            asyncio.get_running_loop().call_later(2.0, lambda: self._mark_recording(name))
            return
        # le signal sonore part quand tout le monde enregistre (au plus tard 4 s après)
        asyncio.get_running_loop().call_later(5.5, lambda: asyncio.ensure_future(self.maybe_chirp(force=True)))

    def _mark_recording(self, name):
        if self.take and self.take["name"] == name and self.take["state"] == "starting":
            self.take["state"] = "recording"
            self.changed()

    async def maybe_chirp(self, force=False):
        tk = self.take
        if not tk or tk.get("kind") == "visage" or tk.get("chirp_at_pc") or tk["state"] not in ("starting", "recording"):
            return
        cams = [self.cams[i] for i in tk["cams"] if i in self.cams]
        if not force and not all(c.take.get("state") == "recording" for c in cams):
            return
        tk["state"] = "recording"
        # certains téléphones (Android) commencent réellement à filmer ~0,5 s après avoir
        # annoncé le départ : on laisse de la marge avant le bip
        await asyncio.sleep(1.2)
        await self.chirp()
        self.changed()

    async def chirp(self):
        tk = self.take
        t = now_ms()
        tk.setdefault("chirps_at_pc", []).append(t)
        if not tk.get("chirp_at_pc"):
            tk["chirp_at_pc"] = t
        if not self.play_chirp_local():
            await self.broadcast(dict(type="play_chirp"))   # joué par l'interface

    def play_chirp_local(self):
        """Joue le signal par les haut-parleurs (Windows). Sinon l'interface s'en charge."""
        try:
            import winsound
        except ImportError:
            return False
        if self.chirp_wav is None:
            sr = 48000
            s = sync.chirp_signal(sr)
            pcm = (np.concatenate([np.zeros(int(0.05 * sr)), s, np.zeros(int(0.05 * sr))]) * 0.9 * 32767).astype(np.int16)
            path = os.path.join(tempfile.gettempdir(), "mocapcam_chirp.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm.tobytes())
            self.chirp_wav = path
        winsound.PlaySound(self.chirp_wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True

    async def stop_take(self):
        tk = self.take
        if not tk or tk["state"] not in ("starting", "recording"):
            return
        tk["state"] = "stopping"
        self.changed()
        # second bip avant l'arrêt : synchro vérifiée deux fois + mesure de dérive
        if tk.get("chirp_at_pc"):
            await self.chirp()
            await asyncio.sleep(0.9)
        tk["stopped_at_pc"] = now_ms()
        for cid in tk["cams"]:
            if cid in self.cams:
                await self.send(self.cams[cid], dict(t="stop"))
        self.changed()

    async def check_take_complete(self):
        tk = self.take
        if not tk or tk["state"] != "stopping":
            return
        cams = [self.cams[i] for i in tk["cams"] if i in self.cams]
        if all(c.take.get("state") in ("done", "error") for c in cams):
            if tk.get("kind") == "visage":
                await self.finish_face_take(tk, cams)
                return
            self.write_take_json()
            n = len([f for f in os.listdir(tk["dir"]) if f.lower().endswith((".mp4", ".mov"))])
            tk["state"] = "ready"
            await self.toast(f"Prise {tk['name']} rapatriée ({n} vidéo(s))")
            self.changed()

    async def finish_face_take(self, tk, cams):
        from . import face as facemod
        c = cams[0] if cams else None
        facemod.write_meta(tk["dir"], take=tk["name"], kind="visage", created=tk["created"],
                           camera=c.name if c else None, facing=c.status.get("facing") if c else None,
                           duration_s=round((tk.get("stopped_at_pc", now_ms()) - tk.get("at_pc", now_ms())) / 1000, 1),
                           fps_camera=c.status.get("fps") if c else None, model=c.model if c else None,
                           miroir=False)
        tk["state"] = "ready"
        if facemod.find_video(tk["dir"]):
            await self.toast(f"Visage {tk['name']} rapatrié : traitement lancé", "ok")
            await self.enqueue_face("visage", tk["name"])
        else:
            await self.toast(f"Visage {tk['name']} : la vidéo n'est pas arrivée", "error")
        self.changed()

    async def enqueue_face(self, kind, name, args=(), label=None):
        name = safe_name(name)
        d = os.path.join(self.faces_dir, name)
        if not os.path.isdir(d):
            await self.toast(f"Prise de visage introuvable : {name}", "error")
            return
        if any(j["kind"] == kind and j["take"] == name and j["args"] == list(args) for j in self.queue):
            return
        self.queue.append(dict(kind=kind, take=name, args=list(args), dir=d,
                               label=label or ("Capture faciale" if kind == "visage" else "Animation du visage")))
        self.changed()
        if not (self.proc and self.proc["running"]):
            asyncio.ensure_future(self.run_queue())

    def snapshot_cam(self, c):
        """Mémorise l'état de la caméra pour la prise en cours (survit à une déconnexion)."""
        tk = self.take
        if not tk or c.take.get("name") != tk["name"]:
            return
        f = c.take.get("file")
        tk.setdefault("meta", {})[c.name] = dict(
            file=os.path.basename(f) if f and os.path.exists(f) else None,
            started_at_pc=c.take.get("started_at_pc"), called_at_pc=c.take.get("called_at_pc"),
            clock_offset_ms=c.offset, sync_rtt_ms=c.rtt, platform=c.platform, model=c.model,
            fps=c.status.get("fps"), stabilization=c.status.get("stabilization"),
            resolution=c.status.get("resolution"), exposure=c.status.get("exposure"), locked=c.locked,
            lens=c.status.get("lens"), zoom=c.status.get("zoom"), fov=c.status.get("fov"))

    def write_take_json(self):
        tk = self.take
        meta = dict(take=tk["name"], created=tk["created"], chirp_at_pc=tk.get("chirp_at_pc"),
                    actor_height_cm=tk.get("actor_height_cm"),
                    chirps_at_pc=tk.get("chirps_at_pc", []),
                    chirp=sync.CHIRP, at_pc=tk.get("at_pc"),
                    duration_s=round((tk.get("stopped_at_pc", now_ms()) - tk.get("at_pc", now_ms())) / 1000, 1),
                    cameras={})
        for cid in tk["cams"]:
            if cid in self.cams:
                self.snapshot_cam(self.cams[cid])
        meta["cameras"] = dict(tk.get("meta", {}))
        # vidéos présentes dans le dossier mais inconnues (caméra déconnectée trop tôt…)
        for f in sorted(os.listdir(tk["dir"])):
            n, ext = os.path.splitext(f)
            if ext.lower() in (".mp4", ".mov") and not f.endswith(".part"):
                meta["cameras"].setdefault(n, {})["file"] = f
        with open(os.path.join(tk["dir"], "take.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, ensure_ascii=False)

    async def handle_upload(self, request):
        q = request.rel_url.query
        if q.get("token") != self.token:
            return web.Response(status=403, text="token")
        cam_name = safe_name(q.get("cam"), "camera").lower()
        take = safe_name(q.get("take"), "prise")
        ext = "mov" if q.get("ext") == "mov" else "mp4"
        d = os.path.join(self.captures_dir, take)
        if self.take and self.take.get("kind") == "visage" and self.take["name"] == take:
            d = self.take["dir"]
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{cam_name}.{ext}")
        tmp = path + ".part"
        size = 0
        with open(tmp, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
                size += len(chunk)
        os.replace(tmp, path)
        meta_path = os.path.join(d, "take.json")
        if os.path.exists(meta_path) and not (self.take and self.take["name"] == take
                                              and self.take["state"] != "ready"):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
                meta.setdefault("cameras", {}).setdefault(cam_name, {})["file"] = os.path.basename(path)
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2, ensure_ascii=False)
                await self.toast(f"Vidéo de {cam_name} ajoutée à {take}")
            except (OSError, ValueError):
                pass
        for c in self.cams.values():
            if c.name == cam_name and c.take.get("name") == take:
                c.take["file"] = path
                c.take["bytes"] = size
        self.changed()
        return web.Response(text="ok")

    # ------------------------------------------------------------------ traitements (file d'attente)
    JOB_LABEL = {"traiter": "Traitement", "taille": "Mise à la taille", "animer": "Animation du personnage"}

    async def enqueue(self, kind, take, args=(), label=None):
        take = safe_name(take)
        d = os.path.join(self.captures_dir, take)
        if not os.path.isdir(d):
            await self.toast(f"Prise introuvable : {take}", "error")
            return
        if any(j["kind"] == kind and j["take"] == take and j["args"] == list(args) for j in self.queue):
            return
        self.queue.append(dict(kind=kind, take=take, args=list(args), label=label or self.JOB_LABEL[kind]))
        if self.proc and self.proc["running"]:
            await self.toast(f"{label or self.JOB_LABEL[kind]} de {take} : en attente")
        self.changed()
        if not (self.proc and self.proc["running"]):
            asyncio.ensure_future(self.run_queue())

    async def run_queue(self):
        while self.queue and not (self.proc and self.proc["running"]):
            job = self.queue.pop(0)
            await self.run_job(job)

    async def run_job(self, job):
        kind, take = job["kind"], job["take"]
        d = job.get("dir") or os.path.join(self.captures_dir, take)
        if kind == "visage":
            cmd = ["visage", d, "--studio", self.studio_dir]
        elif kind == "traiter":
            cmd = ["traiter", self.studio_dir, d]
        elif kind == "taille":
            cmd = ["taille", d, str(job["args"][0]), "--studio", self.studio_dir]
        else:
            cmd = ["animer", self.studio_dir, d, job["args"][0]]
        cmd = [sys.executable, "-u", "-m", "mocapcam", *cmd]
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
        self.proc = dict(take=take, running=True, ok=None, kind=kind, label=job["label"], step=0, step_label="")
        await self.broadcast(dict(type="log", line=f"=== {job['label']} : {take} ===", reset=True))
        self.changed()
        p = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT,
                                                 creationflags=CREATE_NO_WINDOW)
        self.proc["pid"] = p.pid
        async for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            m = re.match(r"\s*(\d)/([346]) (.+)", line)
            if m:
                self.proc.update(step=int(m.group(1)), steps=int(m.group(2)), step_label=m.group(3).rstrip("…. "))
                self.changed()
            await self.broadcast(dict(type="log", line=line))
        rc = await p.wait()
        self.proc.update(running=False, ok=(rc == 0))
        if rc == 0:
            lab = job["label"][:1].lower() + job["label"][1:]     # « animation de Y Bot »
            done = {"traiter": f"Prise {take} traitée", "taille": f"{take} : {lab} terminée",
                    "animer": f"{take} : {lab} terminée", "visage": f"Visage {take} traité"}[kind]
            await self.toast(done, "ok")
        else:
            await self.toast(f"Échec : {job['label'].lower()} de {take} (voir le journal dans Prises)", "error")
        self.changed()
        if self.queue:
            asyncio.ensure_future(self.run_queue())

    # ------------------------------------------------------------------ personnages
    async def handle_character_upload(self, request):
        """POST /character?name=…&filename=… : fichier du personnage (FBX, GLB, .blend ou .zip)."""
        q = request.rel_url.query
        fname = os.path.basename(q.get("filename") or "personnage.fbx")
        ext = os.path.splitext(fname)[1].lower()
        if ext not in (".fbx", ".glb", ".gltf", ".blend", ".zip"):
            return web.json_response(dict(ok=False, message="formats acceptés : FBX, GLB, glTF, .blend ou .zip"))
        base = safe_name(q.get("name") or os.path.splitext(fname)[0], "personnage").lower()
        root = pipeline.characters_dir(self.studio_dir)
        os.makedirs(root, exist_ok=True)
        cid, n = base, 2
        while os.path.exists(os.path.join(root, cid)):
            cid, n = f"{base}_{n}", n + 1
        d = os.path.join(root, cid)
        os.makedirs(d)
        path = os.path.join(d, safe_name(os.path.splitext(fname)[0], "personnage") + ext)
        with open(path, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
        self.char_busy = dict(id=cid, name=q.get("name") or os.path.splitext(fname)[0])
        self.changed()
        try:
            if ext == ".zip":
                with zipfile.ZipFile(path) as z:
                    for m in z.namelist():
                        if ".." in m or m.startswith(("/", "\\")):
                            continue
                        z.extract(m, d)
                os.remove(path)
                models = [os.path.join(dp, f) for dp, _, fs in os.walk(d) for f in fs
                          if os.path.splitext(f)[1].lower() in (".fbx", ".glb", ".gltf", ".blend")]
                if not models:
                    raise RuntimeError("aucun fichier FBX, GLB ou .blend dans le .zip")
                path = sorted(models, key=lambda x: (x.count(os.sep), -os.path.getsize(x)))[0]
            logs = []
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: fbx.inspect_character(
                path, os.path.join(d, "apercu.glb"), self.cfg().get("blender") or None, logs.append))
            if not info:
                raise RuntimeError(" ".join(x.strip() for x in logs)[-300:] or "Blender n'a pas pu ouvrir le fichier")
            meta = dict(name=self.char_busy["name"], file=os.path.relpath(path, d), info=info,
                        created=time.strftime("%Y-%m-%d %H:%M:%S"))
            with open(os.path.join(d, "personnage.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            # premier personnage, sans personnage fourni (Y Bot) : il devient celui par défaut
            if not info.get("face_only") and not self.cfg().get("character") and not any(c.get("builtin") for c in pipeline.list_characters(self.studio_dir)):
                self.save_cfg_value("character", cid)
            await self.toast(f"Personnage « {meta['name']} » ajouté : squelette {info.get('kind')}, "
                             f"{info.get('mapped')} os reconnus", "ok")
            res = dict(ok=True, id=cid)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(d, ignore_errors=True)
            await self.toast(f"Personnage refusé : {e}", "error")
            res = dict(ok=False, message=str(e))
        self.char_busy = None
        self.changed()
        return web.json_response(res)

    async def handle_character_glb(self, request):
        cid = safe_name(request.match_info["cid"]).lower()
        p = os.path.join(pipeline.characters_dir(self.studio_dir), cid, "apercu.glb")
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-store"})

    async def handle_take_glb(self, request):
        take = safe_name(request.match_info["take"])
        cid = safe_name(request.rel_url.query.get("c", "")).lower()
        p = pipeline.character_outputs(os.path.join(self.captures_dir, take), cid)[1]
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-store"})

    def open_folder(self, take=None, path=None):
        d = path or (os.path.join(self.captures_dir, safe_name(take)) if take else self.captures_dir)
        if not os.path.isdir(d):
            return
        if hasattr(os, "startfile"):
            os.startfile(d)  # noqa: S606 (Windows)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def delete_take(self, take):
        return self.delete_dir(os.path.join(self.captures_dir, take))

    def delete_dir(self, d):
        """Supprime un dossier : corbeille de Windows (récupérable), sinon suppression."""
        if sys.platform.startswith("win"):
            ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                  "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($env:MOCAM_DEL, "
                  "'OnlyErrorDialogs', 'SendToRecycleBin')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=dict(os.environ, MOCAM_DEL=d),
                           capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.path.isdir(d):
                raise OSError("la corbeille a refusé le dossier (un fichier est peut-être ouvert)")
            return "corbeille"
        shutil.rmtree(d)
        return "supprimée"

    # ------------------------------------------------------------------ capture faciale
    def face_path(self, request):
        name = safe_name(request.match_info["name"])
        d = os.path.join(self.faces_dir, name)
        if not os.path.isdir(d):
            raise web.HTTPNotFound()
        return name, d

    async def handle_face_video(self, request):
        from . import face as facemod
        _, d = self.face_path(request)
        v = facemod.find_video(d)
        if not v:
            raise web.HTTPNotFound()
        up = facemod.outputs(d)["view"]          # vidéo redressée (filmée couchée)
        return web.FileResponse(up if os.path.exists(up) else v, headers={"Cache-Control": "no-store"})

    async def handle_face_glb(self, request):
        from . import face as facemod
        _, d = self.face_path(request)
        p = facemod.outputs(d)["glb"]
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-store"})

    async def handle_face_curves(self, request):
        """Courbes de la prise de visage pour l'interface (sans le maillage appris)."""
        from . import face as facemod
        _, d = self.face_path(request)
        p = facemod.curves_path(d)
        if not os.path.exists(p):
            raise web.HTTPNotFound()

        def load():
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            data.pop("face", None)
            return data
        data = await asyncio.get_running_loop().run_in_executor(None, load)
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def handle_face_import(self, request):
        """POST /visage/import?name=…&filename=… : vidéo d'un visage filmée à part."""
        from . import face as facemod
        q = request.rel_url.query
        fname = os.path.basename(q.get("filename") or "video.mp4")
        ext = os.path.splitext(fname)[1].lower()
        if ext not in facemod.VIDEO_EXTS:
            return web.json_response(dict(ok=False, message="vidéo attendue : MP4, MOV, M4V, AVI, MKV ou WEBM"))
        name = safe_name(q.get("name") or "", self.next_face_name())
        d, n = os.path.join(self.faces_dir, name), 2
        while os.path.exists(d):
            d, n = os.path.join(self.faces_dir, f"{name}_{n}"), n + 1
        name = os.path.basename(d)
        os.makedirs(d)
        with open(os.path.join(d, "video" + ext), "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
        facemod.write_meta(d, take=name, kind="visage", created=time.strftime("%Y-%m-%d %H:%M:%S"),
                           camera="import", source=fname, miroir=False)
        await self.enqueue_face("visage", name)
        self.changed()
        return web.json_response(dict(ok=True, name=name))

    async def track_live(self, cam, jpg):
        """Aperçu direct du visage : suivi de l'image d'aperçu, valeurs envoyées à l'interface."""
        from concurrent.futures import ThreadPoolExecutor
        from . import face as facemod
        self.face_live_busy = True
        try:
            if self.face_pool is None:
                self.face_pool = ThreadPoolExecutor(1)       # MediaPipe : toujours le même fil
                self.face_tracker = facemod.LiveTracker(log=lambda *a: None)
            res = await asyncio.get_running_loop().run_in_executor(self.face_pool, self.face_tracker, jpg)
            if res is not None and cam.id == self.face_live:
                await self.broadcast(dict(type="face_live", cam=cam.id, **res))
        except Exception as e:  # noqa: BLE001  MediaPipe absent, image illisible…
            await self.broadcast(dict(type="face_live", cam=cam.id, found=False, error=str(e)[:200]))
        finally:
            self.face_live_busy = False

    async def set_face_live(self, cam_id):
        old = self.face_live
        if old == cam_id:
            return
        self.face_live = cam_id if cam_id in self.cams else None
        if old in self.cams:
            await self.send(self.cams[old], dict(t="preview", fps=self.preview_fps))
        if self.face_live:
            if self.face_tracker:
                self.face_tracker.reset()
            await self.send(self.cams[self.face_live], dict(t="preview", fps=30.0))

    async def on_face_ui(self, a, m):
        from . import face as facemod
        name = safe_name(m["name"]) if m.get("name") else ""
        d = os.path.join(self.faces_dir, name) if name else None
        if a == "face_live":
            await self.set_face_live(m.get("cam") if m.get("on") else None)
        elif a == "face_facing":
            c = self.cams.get(m.get("cam"))
            if c:
                await self.send(c, dict(t="config", facing="front" if m.get("facing") == "front" else "back"))
        elif a == "face_record":
            c = self.cams.get(m.get("cam"))
            if not c:
                await self.toast("Choisissez la caméra qui filme le visage", "warn")
                return
            await self.start_take(m.get("name") or "", cam_id=c.id, kind="visage")
        elif a == "face_settings" and d and os.path.isdir(d):
            kw = {}
            if m.get("lissage") in facemod.SMOOTH:
                kw["lissage"] = m["lissage"]
            if isinstance(m.get("intensite"), (int, float)):
                kw["intensite"] = int(min(150, max(50, m["intensite"])))
            facemod.write_meta(d, **kw)
            if m.get("apply"):
                await self.enqueue_face("visage", name)
            self.changed()
        elif a == "face_process" and d:
            await self.enqueue_face("visage", name)
        elif a == "face_open" and d:
            self.open_folder(path=d)
        elif a == "face_delete" and d and os.path.isdir(d):
            busy = (self.proc and self.proc.get("running") and self.proc.get("take") == name) or \
                (self.take and self.take.get("name") == name and self.take.get("state") in ("starting", "recording", "stopping"))
            if busy:
                await self.toast(f"{name} est en cours : attendez la fin", "warn")
                return
            self.queue = [j for j in self.queue if j["take"] != name]
            how = await asyncio.get_running_loop().run_in_executor(None, self.delete_dir, d)
            self.changed()
            await self.toast(f"Visage {name} " + ("mis à la corbeille" if how == "corbeille" else "supprimé"), "ok")

    async def handle_ui(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.uis.add(ws)
        await ws.send_str(json.dumps(self.state()))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                try:
                    await self.on_ui(m)
                except Exception as e:  # noqa: BLE001  une action ratée ne coupe pas la connexion
                    print(f"  action {m.get('a')} : {e!r}")
                    await self.toast(f"Action impossible : {e}", "error")
        finally:
            self.uis.discard(ws)
        return ws

    async def on_ui(self, m):
        a = m.get("a")
        cams = list(self.cams.values())
        if a == "record":
            await self.start_take(m.get("take"))
        elif a == "stop":
            await self.stop_take()
        elif a == "lock":
            for c in cams:
                await self.send(c, dict(t="lock", fast_shutter=bool(m.get("fast"))))
        elif a == "unlock":
            for c in cams:
                await self.send(c, dict(t="unlock"))
        elif a == "check":
            targets = [self.cams[m["cam"]]] if m.get("cam") in self.cams else cams
            for c in targets:
                asyncio.ensure_future(self.request_check(c))
        elif a == "identify" and m.get("cam") in self.cams:
            await self.send(self.cams[m["cam"]], dict(t="identify"))
        elif a == "rename" and m.get("cam") in self.cams:
            c = self.cams[m["cam"]]
            c.name = self.unique_name(m.get("name", c.name), c.id)
            await self.send(c, dict(t="rename", name=c.name))
            self.changed()
        elif a == "kick" and m.get("cam") in self.cams:
            await self.cams[m["cam"]].ws.close()
        elif a == "preview":
            self.preview_fps = float(m.get("fps", 3))
            for c in cams:
                await self.send(c, dict(t="preview", fps=self.preview_fps))
            self.changed()
        elif a == "fov" and m.get("cam") in self.cams:
            c = self.cams[m["cam"]]
            try:
                v = float(m.get("value"))
            except (TypeError, ValueError):
                return
            v = min(1.0, max(0.3, v))     # on élargit seulement (×1 = objectif principal)
            self.fov_by_name[c.name] = v
            self._save_fov()
            await self.send(c, dict(t="config", fov=v, lens="ultra" if v < 0.99 else "wide"))
            c.check = None   # la vérification des boules est à refaire
            self.changed()
        elif a == "prefer60":
            self.prefer60 = bool(m.get("value"))
            for c in cams:
                await self.send(c, dict(t="config", prefer60=self.prefer60))
            self.changed()
        elif a == "process":
            await self.enqueue("traiter", m.get("take"))
        elif a == "cancel_queue":
            self.queue = [j for j in self.queue if j["take"] != m.get("take")]
            self.changed()
        elif a and a.startswith("face_"):
            await self.on_face_ui(a, m)
        elif a == "open":
            self.open_folder(m.get("take"))
        elif a == "take_delete":
            take = safe_name(m.get("take", ""))
            d = os.path.join(self.captures_dir, take)
            busy = (self.proc and self.proc.get("running") and self.proc.get("take") == take) \
                or (self.take and self.take.get("name") == take and self.take.get("state") in ("starting", "recording", "stopping"))
            if not take or not os.path.isdir(d):
                return
            if busy:
                await self.toast(f"{take} est en cours d'enregistrement ou de traitement : attendez la fin", "warn")
                return
            self.queue = [j for j in self.queue if j["take"] != take]
            how = await asyncio.get_running_loop().run_in_executor(None, self.delete_take, take)
            self.changed()
            await self.toast(f"Prise {take} " + ("mise à la corbeille" if how == "corbeille" else "supprimée"), "ok")
        elif a == "open_characters":
            os.makedirs(pipeline.characters_dir(self.studio_dir), exist_ok=True)
            self.open_folder(path=pipeline.characters_dir(self.studio_dir))
        elif a == "actor_height":
            try:
                v = m.get("cm")
                v = None if v in (None, "") else round(float(v), 1)
                if v is not None and not 50 <= v <= 260:
                    raise ValueError
                self.save_cfg_value("actor_height_cm", v)
            except (TypeError, ValueError):
                await self.toast("Taille entre 50 et 260 cm", "error")
            self.changed()
        elif a == "take_height":
            take = safe_name(m.get("take"))
            d = os.path.join(self.captures_dir, take)
            try:
                v = round(float(m.get("cm")), 1)
                if not 50 <= v <= 260:
                    raise ValueError
            except (TypeError, ValueError):
                await self.toast("Taille entre 50 et 260 cm", "error")
                return
            if os.path.exists(os.path.join(d, "mocapcam", f"{take}.mocap.json")):
                await self.enqueue("taille", take, [v], label=f"Mise à {v:g} cm")
            elif os.path.isdir(d):
                mp = os.path.join(d, "take.json")
                meta = {}
                if os.path.exists(mp):
                    with open(mp, encoding="utf-8") as f:
                        meta = json.load(f)
                meta["actor_height_cm"] = v
                with open(mp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)
                await self.toast(f"{take} : taille {v:g} cm, utilisée au traitement", "ok")
            self.changed()
        elif a == "character_default":
            cid = m.get("id") or ""
            if cid and pipeline.get_character(self.studio_dir, cid) is None:
                return
            self.save_cfg_value("character", cid)
            self.changed()
        elif a == "character_delete":
            cid = safe_name(m.get("id", "")).lower()
            d = os.path.join(pipeline.characters_dir(self.studio_dir), cid)
            ch = pipeline.get_character(self.studio_dir, cid)
            if ch and ch.get("builtin"):
                await self.toast(f"{ch.get('name')} est fourni avec MOCAM : il reste disponible", "warn")
                return
            if cid and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                if self.cfg().get("character") == cid:
                    self.save_cfg_value("character", "")
                await self.toast("Personnage supprimé (les FBX déjà animés restent dans les prises)")
            self.changed()
        elif a == "animate":
            cid = m.get("id") or pipeline.default_character(self.studio_dir, self.cfg())
            ch = pipeline.get_character(self.studio_dir, cid) if cid else None
            if ch is None:
                await self.toast("Ajoutez d'abord un personnage (onglet Personnages)", "error")
                return
            await self.enqueue("animer", m.get("take"), [cid], label=f"Animation de {ch.get('name', cid)}")
        elif a == "firewall":
            await asyncio.get_running_loop().run_in_executor(None, allow_firewall, self.port)
            self.net = await asyncio.get_running_loop().run_in_executor(None, network_info, self.port)
            await self.toast("Pare-feu : MOCAM autorisé sur tous les réseaux" if self.net.get("firewall_rule")
                             else "Pare-feu non modifié (accord administrateur refusé ?)",
                             "ok" if self.net.get("firewall_rule") else "warn")
            self.changed()
        elif a == "square":
            try:
                side = float(m.get("side"))
                if not 0.5 <= side <= 8:
                    raise ValueError("le côté doit être entre 0,5 et 8 m")
                self.set_square_side(side)
                for c in cams:
                    c.check = None
                await self.toast(f"Carré de {side:.2f} m enregistré dans studio.json. "
                                 "Retraitez les prises filmées avec cette disposition.", "ok")
            except (TypeError, ValueError) as e:
                await self.toast(f"Côté du carré non enregistré : {e}", "error")
            self.changed()
        elif a == "new_qr":
            self.token = secrets.token_hex(4)
            self.changed()

    async def handle_preview(self, request):
        c = self.cams.get(request.match_info["cam"])
        if not c or not c.preview:
            raise web.HTTPNotFound()
        return web.Response(body=c.preview, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def handle_check(self, request):
        c = self.cams.get(request.match_info["cam"])
        if not c or not c.check_jpg:
            raise web.HTTPNotFound()
        return web.Response(body=c.check_jpg, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def handle_qr(self, request):
        import io
        import segno
        buf = io.BytesIO()
        segno.make(json.dumps(self.qr_payload(), separators=(",", ":")), error="m").save(
            buf, kind="svg", scale=8, dark="#0b0d12", light="#fff", border=2, xmldecl=True)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    async def handle_version(self, request):
        return web.json_response(dict(app="MOCAM Studio", version=STUDIO_VERSION, pid=os.getpid()))

    async def handle_index(self, request):
        return web.FileResponse(os.path.join(UI_DIR, "index.html"), headers={"Cache-Control": "no-store"})

    def app(self):
        app = web.Application(client_max_size=8 * 1024 ** 3)
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/visage/{name}/video", self.handle_face_video)
        app.router.add_get("/visage/{name}/apercu.glb", self.handle_face_glb)
        app.router.add_get("/visage/{name}/courbes.json", self.handle_face_curves)
        app.router.add_post("/visage/import", self.handle_face_import)
        app.router.add_get("/version", self.handle_version)
        app.router.add_get("/ws/ui", self.handle_ui)
        app.router.add_get("/ws/phone", self.handle_phone)
        app.router.add_post("/upload", self.handle_upload)
        app.router.add_get("/preview/{cam}", self.handle_preview)
        app.router.add_get("/check/{cam}", self.handle_check)
        app.router.add_get("/qr.svg", self.handle_qr)
        app.router.add_post("/character", self.handle_character_upload)
        app.router.add_get("/character/{cid}/apercu.glb", self.handle_character_glb)
        app.router.add_get("/take/{take}/apercu.glb", self.handle_take_glb)
        for img in ("logo-mocam.png", "favicon.png", "mocam.ico"):
            app.router.add_get("/" + img, lambda r, img=img: web.FileResponse(
                os.path.join(UI_DIR, img), headers={"Cache-Control": "max-age=3600"}))
        app.router.add_static("/vendor/", os.path.join(UI_DIR, "vendor"), show_index=False)

        async def on_startup(app):
            app["pusher"] = asyncio.create_task(self.state_pusher())
            app["net"] = asyncio.create_task(self.net_loop())
            app["bundled"] = asyncio.create_task(self.install_bundled())

        app.on_startup.append(on_startup)
        return app


def open_window(url):
    """Ouvre l'interface dans une fenêtre d'application Edge (Windows), sinon le navigateur."""
    if sys.platform.startswith("win"):
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}", "--window-size=1500,950"],
                             creationflags=0x08000000)
            return
        except OSError:
            pass
    webbrowser.open(url)


def make_shortcuts():
    """Windows : raccourci « MOCAM Studio » avec l'icône MOCAM, dans le dossier du programme et sur
    le Bureau (créé une fois ; un .bat ne peut pas avoir d'icône)."""
    if not sys.platform.startswith("win"):
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bat, ico = os.path.join(root, "MOCAM Studio.bat"), os.path.join(UI_DIR, "mocam.ico")
    if not (os.path.exists(bat) and os.path.exists(ico)):
        return
    ps = ("$s=New-Object -ComObject WScript.Shell; "
          "foreach($d in @($env:MOCAM_ROOT, [Environment]::GetFolderPath('Desktop'))){ "
          "$p=Join-Path $d 'MOCAM Studio.lnk'; $l=$s.CreateShortcut($p); "
          "$l.TargetPath=$env:MOCAM_BAT; $l.WorkingDirectory=$env:MOCAM_ROOT; "
          "$l.IconLocation=$env:MOCAM_ICO; $l.WindowStyle=7; $l.Description='MOCAM Studio'; $l.Save() }")
    done = os.path.join(root, "studio", ".raccourcis")
    if os.path.exists(done) and os.path.exists(os.path.join(root, "MOCAM Studio.lnk")):
        return
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=30, capture_output=True,
                       env=dict(os.environ, MOCAM_ROOT=root, MOCAM_BAT=bat, MOCAM_ICO=ico),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        os.makedirs(os.path.dirname(done), exist_ok=True)
        open(done, "w").close()
        print("  raccourci « MOCAM Studio » (avec son icône) créé sur le Bureau et dans le dossier MOCAM")
    except (OSError, subprocess.SubprocessError):
        pass


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not sys.platform.startswith("win"):     # connexions en TIME_WAIT : le port reste libre
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _running_studio(port):
    """Version du MOCAM Studio qui occupe déjà le port ("ancienne" s'il ne la donne pas), ou None."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("version") or "ancienne"
    except urllib.error.HTTPError:
        try:      # pas de /version : une version d'avant 1.2 répond quand même sur /qr.svg
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/qr.svg", timeout=3):
                return "ancienne"
        except (OSError, ValueError):
            return None
    except (OSError, ValueError):
        return None


def _listening_pids(port):
    """Windows : processus qui écoutent sur le port."""
    rc, out = _run(["netstat", "-ano", "-p", "tcp"])
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() in ("LISTENING", "ÉCOUTE", "ECOUTE"):
            if parts[4].isdigit() and int(parts[4]) != os.getpid():
                pids.add(int(parts[4]))
    if not pids:
        rc, out = _run(["powershell", "-NoProfile", "-Command",
                        f"(Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess"])
        pids = {int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()}
    return pids


def _stop_old_studio(port):
    """Ferme un MOCAM Studio d'une autre version qui occupe le port. True si le port est libéré."""
    if not sys.platform.startswith("win"):
        return False
    for pid in _listening_pids(port):
        rc, out = _run(["powershell", "-NoProfile", "-Command",
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"])
        if "mocapcam" not in out.lower():
            continue
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
    for _ in range(40):
        if _port_free(port):
            return True
        time.sleep(0.25)
    return False


def run(studio_dir, captures_dir, port=8765, open_ui=True):
    url = f"http://localhost:{port}"
    if not _port_free(port):
        other = _running_studio(port)
        if other == STUDIO_VERSION:
            print(f"MOCAM Studio est déjà ouvert ({url}) : sa fenêtre est réaffichée.")
            if open_ui:
                open_window(url)
            return
        if other is None:
            pass        # rien ne répond (port en cours de libération) : on tente de démarrer
        elif _stop_old_studio(port):
            print(f"Un MOCAM Studio d'une autre version ({other}) tournait encore : il a été fermé "
                  "et remplacé par celui-ci. Vous pouvez fermer son ancienne fenêtre noire.")
        else:
            print(f"Le port {port} est déjà utilisé" + (f" par MOCAM Studio ({other})" if other else
                                                        " par un autre programme") +
                  " : fermez-le (ou fermez les autres fenêtres noires de MOCAM), puis relancez.")
            return
    threading_sc = __import__("threading").Thread(target=make_shortcuts, daemon=True)
    threading_sc.start()
    st = Studio(studio_dir, captures_dir, port)
    print(f"MOCAM Studio {STUDIO_VERSION} : {url}")
    print(f"  réseau : {', '.join(st.net.get('hosts') or lan_addresses())} (port {port}) — connectez les téléphones avec le QR code")
    print(f"  prises enregistrées dans : {st.captures_dir}")
    print("  Laissez cette fenêtre ouverte. Ctrl+C pour quitter.")
    if open_ui:
        threading_open = __import__("threading").Timer(1.0, open_window, args=(url,))
        threading_open.start()
    web.run_app(st.app(), host="0.0.0.0", port=port, print=None, access_log=None)
