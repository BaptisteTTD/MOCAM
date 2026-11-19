"""Test de MOCAM Studio avec deux faux téléphones et un vrai navigateur (Playwright).

Les faux téléphones ont chacun une horloge décalée et un délai réseau ; ils envoient
aperçus et images de contrôle (sol + boules rendus), enregistrent une vidéo dont la
bande-son contient le signal de synchro au bon instant, et l'envoient au PC.
"""
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import time

import cv2
import numpy as np
from aiohttp import ClientSession, WSMsgType

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_synthetique as ts  # noqa: E402
from mocapcam import pipeline, sync, video  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
D = tempfile.mkdtemp(prefix="mocapcam_studio_")
STUDIO, CAPT = os.path.join(D, "studio"), os.path.join(D, "captures")
RESULTS = {}
SHOTS = []


def ok(name, cond, detail):
    RESULTS[name] = (bool(cond), detail)
    print(f"[{'OK' if cond else 'ÉCHEC'}] {name} : {detail}")


MLIST = [dict(m, position=[m["position"][0], m["position"][1], 0.01]) for m in pipeline.DEFAULT_STUDIO["markers"]]


class FakePhone:
    def __init__(self, name, ci, clock_offset_ms, net_ms, crash_after_upload=False):
        self.name, self.ci = name, ci
        self.crash_after_upload = crash_after_upload
        self.clock = clock_offset_ms
        self.net = net_ms
        R, t = ts.CAMS_TRUE[ci]
        self.frame = ts.render_floor(R, t, MLIST, ts.K_TRUE[ci], ts.DIST_TRUE[ci])
        self.small = cv2.resize(self.frame, (640, 360))
        self.rec_start = None    # heure PC réelle du début d'enregistrement
        self.rec_stop = None
        self.take = None
        self.ws = None
        self.recording = False
        self.fov = 1.0
        self.snapshots = 0
        # le poco « rate » la première demande d'image (occupé à envoyer l'aperçu) :
        # Studio doit la redemander tout seul
        self.ignore_first_snapshot = ci == 1

    def now(self):
        return time.time() * 1000 + self.clock

    async def lag(self):
        await asyncio.sleep(random.uniform(0.3, 1.0) * self.net / 1000)

    async def send(self, m):
        await self.lag()
        await self.ws.send_str(json.dumps(m))

    async def send_bin(self, kind, img, q=60):
        ok_, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        await self.ws.send_bytes(bytes([kind]) + enc.tobytes())

    async def run(self, info, session, harness):
        host = info["hosts"][0]
        self.ws = await session.ws_connect(f"ws://{host}:{info['port']}/ws/phone")
        await self.ws.send_str(json.dumps(dict(t="hello", token=info["token"], name=self.name,
                                               platform="ios" if self.ci == 0 else "android", model="faux")))

        async def status_loop():
            while not self.ws.closed:
                await self.ws.send_str(json.dumps(dict(
                    t="status", battery=0.8 - 0.3 * self.ci, storage_free=20e9, fps=60, stabilization="off",
                    exposure=dict(duration=1 / 500, iso=400, mode="locked", focus="locked"),
                    recording=self.recording, locked=True, device="Back Camera", fov=self.fov,
                    fov_options=[0.5, 0.7, 1, 1.5, 2], lens="ultra" if self.fov < 1 else "wide", app="1.1.0")))
                await self.send_bin(1, self.small)
                await asyncio.sleep(0.5)

        st = asyncio.create_task(status_loop())
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            t = m["t"]
            if t == "ping":
                await self.lag()
                c = self.now()
                await self.lag()
                await self.ws.send_str(json.dumps(dict(t="pong", s0=m["s0"], c=c)))
            elif t == "welcome":
                self.name = m["name"]
                if "fov" in m:
                    self.fov = m["fov"]
            elif t == "config":
                if "fov" in m:
                    self.fov = m["fov"]
            elif t == "snapshot":
                self.snapshots += 1
                if self.ignore_first_snapshot and self.snapshots == 1:
                    continue
                await self.send_bin(2, self.frame, 92)
            elif t == "start":
                asyncio.create_task(self.start(m))
            elif t == "stop":
                asyncio.create_task(self.stop(session, info, harness))
        st.cancel()

    async def start(self, m):
        self.take = m["take"]
        wait = (m["at"] - self.now()) / 1000
        await asyncio.sleep(max(0, wait))
        called = self.now()
        await asyncio.sleep(random.uniform(0.05, 0.35))   # latence de démarrage de la caméra
        self.rec_start = time.time() * 1000
        self.recording = True
        await self.send(dict(t="recording", take=self.take, called_at=called, started_at=self.now()))

    async def stop(self, session, info, harness):
        await asyncio.sleep(random.uniform(0.02, 0.1))
        self.rec_stop = time.time() * 1000
        self.recording = False
        dur = (self.rec_stop - self.rec_start) / 1000
        await self.send(dict(t="recorded", take=self.take, duration=dur, size=0))
        # vidéo : décor fixe + son avec le signal au moment où le PC l'a joué
        path = await asyncio.get_running_loop().run_in_executor(None, self.make_video, dur, list(harness["chirps"]))
        data = open(path, "rb").read()
        url = (f"http://{info['hosts'][0]}:{info['port']}/upload?token={info['token']}"
               f"&cam={self.name}&take={self.take}&ext=mp4")
        await self.send(dict(t="upload", take=self.take, sent=len(data) // 2, total=len(data)))
        async with session.post(url, data=data) as r:
            assert r.status == 200, await r.text()
        if self.crash_after_upload:
            # comme l'app 1.0.2 sur iPhone : erreur juste après l'envoi -> déconnexion
            await self.ws.close()
            return
        await self.send(dict(t="uploaded", take=self.take))

    def make_video(self, dur, chirps_real_ms):
        fps, sr = 30, 48000
        raw = os.path.join(D, f"{self.name}_raw.mp4")
        vw = cv2.VideoWriter(raw, cv2.VideoWriter_fourcc(*"mp4v"), fps, (1280, 720))
        fr = cv2.resize(self.frame, (1280, 720))
        for _ in range(int(dur * fps)):
            vw.write(fr)
        vw.release()
        rng = np.random.default_rng(self.ci)
        a = rng.normal(0, 0.03, int(sr * dur)).astype(np.float32)
        ch = sync.chirp_signal(sr) * 0.15
        for cr in chirps_real_ms:
            tc = (cr - self.rec_start) / 1000 + 0.004 * self.ci  # + trajet du son (~1,4 m)
            i = int(tc * sr)
            if 0 <= i < a.size:
                a[i:i + ch.size] += ch[: max(0, a.size - i)]
        wav = os.path.join(D, f"{self.name}.raw")
        (np.clip(a, -1, 1) * 32767).astype(np.int16).tofile(wav)
        out = os.path.join(D, f"{self.name}_final.mp4")
        subprocess.run([video.ffmpeg_exe(), "-y", "-loglevel", "error", "-i", raw, "-f", "s16le", "-ar", str(sr),
                        "-ac", "1", "-i", wav, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        "-shortest", out], check=True)
        return out


async def ui_state(session):
    async with session.ws_connect(f"ws://127.0.0.1:{PORT}/ws/ui") as ws:
        async for msg in ws:
            m = json.loads(msg.data)
            if m.get("type") == "state":
                return m


async def main():
    env = dict(os.environ, PYTHONPATH=ROOT)
    srv = subprocess.Popen([sys.executable, "-m", "mocapcam", "studio", "--sans-fenetre", "--port", str(PORT),
                            "--studio", STUDIO, "--captures", CAPT], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    from playwright.async_api import async_playwright
    try:
        async with ClientSession() as session:
            for _ in range(50):
                try:
                    st = await ui_state(session)
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.2)
            info = st["qr"]
            info["hosts"] = ["127.0.0.1"]
            harness = dict(chirps=[])
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
                page = await browser.new_page(viewport=dict(width=1500, height=950))
                page_errors = []
                page.on("pageerror", lambda e: page_errors.append(str(e)))
                chirps = []
                await page.expose_function("onChirp", lambda: chirps.append(time.time() * 1000))
                await page.add_init_script("window.addEventListener('load',()=>{const o=window.playChirp;"
                                           "window.playChirp=function(){window.onChirp();o&&o();};});")
                await page.goto(BASE)
                await page.wait_for_timeout(800)
                # interface et serveur de la même version, QR code affiché avant tout téléphone
                fatal = await page.locator("#fatal").is_visible()
                qr_w = await page.evaluate("(() => { const i = document.querySelector('img.qr'); return i ? i.naturalWidth : 0 })()")
                async with session.get(BASE + "/version") as r:
                    ver = (await r.json()).get("version")
                ok("page sans erreur JavaScript", not page_errors, "; ".join(page_errors)[:200] or "aucune")
                ok("interface affichée, QR code visible dès l'ouverture", not fatal and qr_w > 50 and ver,
                   f"version {ver}, QR {qr_w} px")
                # 2e lancement de la même version : ne casse rien, se contente de rouvrir la fenêtre
                second = subprocess.run([sys.executable, "-m", "mocapcam", "studio", "--sans-fenetre", "--port", str(PORT),
                                         "--studio", STUDIO, "--captures", CAPT], cwd=ROOT, env=env,
                                        capture_output=True, text=True, timeout=30)
                ok("relance de Studio déjà ouvert", second.returncode == 0 and "déjà ouvert" in second.stdout
                   and srv.poll() is None, second.stdout.strip()[-120:])
                # serveur d'une autre version : l'interface le dit au lieu de rester vide
                await page.evaluate("ws.onmessage({data: JSON.stringify({...S, type: 'state', version: '0.9'})})")
                fatal_txt = await page.locator("#fatal").inner_text() if await page.locator("#fatal").is_visible() else ""
                ok("ancienne version du serveur signalée", "relancé" in fatal_txt, fatal_txt.splitlines()[0] if fatal_txt else "rien d'affiché")
                await page.reload()
                await page.wait_for_timeout(800)
                # sans personnage : une indication, plus de mannequin de remplacement
                await page.locator(".tab", has_text="Personnages").click()
                await page.wait_for_timeout(1500)
                grid = await page.locator("#charGrid").inner_text()
                ok("Personnages vide : indication claire", "Aucun personnage" in grid and await page.locator(".char").count() == 0,
                   grid.replace("\n", " | ")[:110])
                await page.screenshot(path=os.path.join(D, "studio_personnages.png"))
                await page.locator(".tab", has_text="Tournage").click()
                phones = [FakePhone("iphone", 0, 123456.0, 15, crash_after_upload=True), FakePhone("poco", 1, -98765.0, 40)]
                tasks = [asyncio.create_task(ph.run(info, session, harness)) for ph in phones]
                await page.wait_for_timeout(3500)
                st = await ui_state(session)
                syncs = {c["name"]: c["sync_ms"] for c in st["cams"]}
                ok("connexion des téléphones (QR + jeton)", len(st["cams"]) == 2, f"caméras : {syncs} (± ms)")
                # un seul clic : les deux caméras sont vérifiées, même celle qui rate la 1re demande
                await page.locator(".checks button", has_text="Vérifier").click()
                await page.wait_for_timeout(9000)
                st = await ui_state(session)
                chk = {c["name"]: c["check"] for c in st["cams"]}
                good = all(v and v.get("ok") and v["seen"] >= 4 for v in chk.values())
                ok("vérification des boules en un clic (image redemandée si perdue)", good and phones[1].snapshots >= 2,
                   "; ".join(f"{k}: {v.get('seen')}/{v.get('total')} boules, erreur {v.get('rms')} px, caméra à {v.get('center')}"
                             for k, v in chk.items()) + f" ; poco : {phones[1].snapshots} demandes")
                # champ de vision choisi dans Studio -> téléphone, retenu pour la prochaine connexion
                await page.locator("#card-" + [c["id"] for c in st["cams"] if c["name"] == "iphone"][0]
                                   + " .seg button", has_text="×0,5").click()
                await page.wait_for_timeout(1500)
                st = await ui_state(session)
                fovs = {c["name"]: c["fov"] for c in st["cams"]}
                saved = json.load(open(os.path.join(STUDIO, "objectifs.json"), encoding="utf-8"))
                ok("champ de vision depuis Studio", phones[0].fov == 0.5 and fovs["iphone"] == 0.5 and saved.get("iphone") == 0.5,
                   f"téléphone ×{phones[0].fov}, affiché {fovs}, mémorisé {saved}")
                # taille de l'acteur en cm -> studio.json, puis take.json de la prise
                await page.fill("#actorH", "182")
                await page.locator("#takeName").click()
                await page.wait_for_timeout(1200)
                cfg1 = json.load(open(os.path.join(STUDIO, "studio.json"), encoding="utf-8"))
                ok("taille de l'acteur saisie en cm", cfg1.get("actor_height_cm") == 182,
                   f"studio.json : actor_height_cm = {cfg1.get('actor_height_cm')}")
                # QR code : adresses du PC, sans IP à taper
                txt = await page.locator(".add").inner_text()
                ok("connexion par QR code uniquement", "Scannez" in txt and "127.0.0.1" not in txt and str(PORT) not in txt,
                   "texte de la carte : " + " / ".join(x for x in txt.splitlines() if x.strip())[:200])
                shot1 = os.path.join(D, "studio_avant.png")
                await page.screenshot(path=shot1)
                SHOTS.append(shot1)

                # enregistrement
                await page.locator("#recArea button.recbtn").click()
                t_click = time.time() * 1000
                for _ in range(80):
                    await page.wait_for_timeout(100)
                    if chirps:
                        break
                harness["chirps"] = chirps   # même liste : le bip d'arrêt s'y ajoutera
                await page.wait_for_timeout(1500)
                shot2 = os.path.join(D, "studio_enregistrement.png")
                await page.screenshot(path=shot2)
                SHOTS.append(shot2)
                ok("signal sonore joué au départ", bool(chirps),
                   f"{(chirps[0] - t_click) / 1000:.2f} s après le clic" if chirps else "aucun")
                await page.locator("#recArea button.recbtn.stop").click()
                for _ in range(120):
                    await page.wait_for_timeout(250)
                    st = await ui_state(session)
                    if st["take"] and st["take"]["state"] == "ready":
                        break
                take = st["take"]["name"]
                tdir = os.path.join(CAPT, take)
                files = sorted(f for f in os.listdir(tdir) if f.endswith(".mp4"))
                meta = json.load(open(os.path.join(tdir, "take.json"), encoding="utf-8"))
                ok("rapatriement des vidéos + take.json (iPhone déconnecté juste après l'envoi)",
                   files == ["iphone.mp4", "poco.mp4"] and meta["chirp_at_pc"] and meta.get("actor_height_cm") == 182
                   and meta["cameras"]["iphone"].get("fov") == 0.5
                   and sorted(meta["cameras"]) == ["iphone", "poco"]
                   and meta["cameras"]["iphone"].get("started_at_pc"),
                   f"{take} : {files}, caméras dans take.json : {sorted(meta['cameras'])}")
                # écart de démarrage estimé (horloges) vs réel
                est = {n: c["started_at_pc"] for n, c in meta["cameras"].items()}
                real = {ph.name: ph.rec_start for ph in phones}
                err = abs((est["poco"] - est["iphone"]) - (real["poco"] - real["iphone"]))
                ok("horodatage des départs (horloges synchronisées)", err < 60,
                   f"écart de départ estimé {est['poco'] - est['iphone']:.0f} ms, réel {real['poco'] - real['iphone']:.0f} ms (erreur {err:.0f} ms)")
                await page.wait_for_timeout(600)
                # onglet Prises, bouton « Traiter la prise » : progression par étapes + journal
                # (ici il s'arrête à l'étape 4 : les modèles de détection ne sont pas téléchargeables)
                await page.locator(".tab", has_text="Prises").click()
                await page.locator(".tk", has_text=take).click()
                await page.locator("button", has_text="Traiter la prise").click()
                steps_seen = set()
                for _ in range(240):
                    await page.wait_for_timeout(250)
                    stx = await ui_state(session)
                    if stx["processing"] and stx["processing"].get("step"):
                        steps_seen.add(stx["processing"]["step"])
                    txt = await page.locator("#log").text_content()
                    if "4/6" in txt or "Terminé" in txt or "ERREUR" in txt or "Error" in txt:
                        break
                ok("bouton Traiter + étapes + journal en direct", "1/6" in txt and "3/6" in txt and 3 in steps_seen,
                   f"étapes vues {sorted(steps_seen)} ; " +
                   " / ".join(l for l in txt.splitlines() if l.strip().startswith(("1/6", "2/6", "3/6", "4/6")))[:300])
                # capture du visage : une seule caméra, sans bip, rangée dans « visages »
                st0 = await ui_state(session)
                cam0 = st0["cams"][0]
                await page.evaluate(f"send({{a:'face_facing', cam:'{cam0['id']}', facing:'front'}})")
                await page.evaluate(f"send({{a:'face_record', cam:'{cam0['id']}'}})")
                await page.wait_for_timeout(3500)
                stx = await ui_state(session)
                one = stx["take"] and stx["take"].get("kind") == "visage" and len(stx["take"]["cams"]) == 1
                await page.evaluate("send({a:'stop'})")
                fdir = os.path.join(os.path.dirname(CAPT), "visages")
                vid = None
                for _ in range(40):
                    await page.wait_for_timeout(500)
                    names = os.listdir(fdir) if os.path.isdir(fdir) else []
                    vids = [f for n in names for f in os.listdir(os.path.join(fdir, n)) if f.endswith((".mp4", ".mov"))]
                    if vids and any(os.path.exists(os.path.join(fdir, n, "take.json")) for n in names):
                        vid = vids[0]
                        break
                fmeta = {}
                if vid:
                    with open(os.path.join(fdir, names[0], "take.json")) as fh:
                        fmeta = json.load(fh)
                stx = await ui_state(session)
                ok("capture du visage avec une seule caméra", one and vid and fmeta.get("kind") == "visage"
                   and any(f["name"] == names[0] for f in stx["faces"]),
                   f"{names[0] if vid else '?'} : {vid}, caméra {fmeta.get('camera')}, {len(stx['faces'])} visage(s) listé(s)")
                # « Ouvrir le dossier » ne coupe pas la connexion ; suppression d'une prise
                await page.locator("#takeDetail button", has_text="Ouvrir le dossier").click()
                await page.wait_for_timeout(1500)
                disc = await page.locator("#disc").is_visible()
                dd = os.path.join(CAPT, "a_supprimer")
                os.makedirs(dd, exist_ok=True)
                with open(os.path.join(dd, "take.json"), "w") as f:
                    json.dump({"take": "a_supprimer"}, f)
                await page.evaluate("send({a:'open'})")      # provoque un nouvel état
                await page.wait_for_timeout(1500)
                await page.evaluate("selectTake('a_supprimer')")
                await page.wait_for_timeout(500)
                page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
                await page.locator("#takeDetail button", has_text="Supprimer la prise").click()
                for _ in range(20):
                    await page.wait_for_timeout(300)
                    if not os.path.isdir(dd):
                        break
                listed = any(t["name"] == "a_supprimer" for t in (await ui_state(session))["takes"])
                ok("ouvrir le dossier puis supprimer une prise", not disc and not os.path.isdir(dd) and not listed,
                   f"connexion {'perdue' if disc else 'gardée'}, dossier {'encore là' if os.path.isdir(dd) else 'supprimé'}")
                shot3 = os.path.join(D, "studio_fin.png")
                await page.screenshot(path=shot3)
                SHOTS.append(shot3)
                await browser.close()
            for tsk in tasks:
                tsk.cancel()
        # synchro précise par le signal sonore, via le vrai pipeline
        norm = {n: video.normalize(os.path.join(tdir, f"{n}.mp4"), os.path.join(D, f"norm_{n}.mp4"), 60, 1080)
                for n in ("iphone", "poco")}
        logs = []
        delays = pipeline.compute_delays(norm, None, meta, log=logs.append)
        true_d = -(real["poco"] - real["iphone"]) / 1000 + 0.004
        e = abs(delays[1] - true_d) * 1000
        ok(f"synchro précise par les bips ({len(meta.get('chirps_at_pc', []))} bips)", e < 2 and len(meta.get("chirps_at_pc", [])) == 2,
           f"décalage trouvé {delays[1] * 1000:.1f} ms, réel {true_d * 1000:.1f} ms (erreur {e:.2f} ms)")
        # étape « marqueurs » du traitement sur la prise Studio
        out = []
        pipeline.process_take(STUDIO, tdir, only_calib=True, log=out.append)
        ok("traitement de la prise Studio (étapes 1 à 3)", any("caméra à" in l for l in out),
           " | ".join(l.strip() for l in out if "caméra à" in l or "décalage" in l))
    finally:
        srv.terminate()
        try:
            print(srv.communicate(timeout=5)[0][-1500:])
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
    n = sum(v[0] for v in RESULTS.values())
    print(f"\n{n}/{len(RESULTS)} tests réussis ; captures d'écran : {SHOTS}")
    sys.exit(0 if n == len(RESULTS) else 1)
