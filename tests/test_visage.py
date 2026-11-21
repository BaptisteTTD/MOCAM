"""Capture faciale : calibration, visage appris, paupières, FBX à 52 blendshapes, aperçu direct.

    python tests/test_visage.py [VIDEO]

Sans vidéo, le suivi MediaPipe est remplacé par un suivi simulé (expressions connues) ; avec une
vidéo de visage, toute la chaîne tourne (il faut le modèle modeles/face_landmarker.task).
Nécessite le module bpy (pip install bpy) pour la partie Blender."""
import json
import os
import sys
import tempfile
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from mocapcam import face  # noqa: E402

RESULTS = []


def ok(name, cond, detail=""):
    RESULTS.append(cond)
    print(f"[{'OK' if cond else 'ÉCHEC'}] {name} : {detail}")


def fake_take(d, n=300, fps=30.0):
    """Suivi simulé : 51 formes connues, points du visage déformés par une base connue, œil gauche
    du sujet fermé (EAR) sur les images 100 à 110, tête qui tourne de ±12°."""
    os.makedirs(os.path.join(d, "mocapcam"), exist_ok=True)
    vw = cv2.VideoWriter(os.path.join(d, "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 64))
    for _ in range(5):
        vw.write(np.zeros((64, 64, 3), np.uint8))
    vw.release()
    time.sleep(1.1)
    rng = np.random.default_rng(1)
    names = [x for x in face.ARKIT if x != "tongueOut"]
    t = np.arange(n) / fps
    W = np.clip(0.06 + 0.45 * np.sin(t[:, None] * np.linspace(0.4, 2.5, 51)[None]) ** 2
                + rng.normal(0, 0.03, (n, 51)), 0, 1)
    W[100:111, names.index("eyeBlinkLeft")] = 0.6      # MediaPipe plafonne vers 0,6, œil fermé
    neutral = rng.normal(0, 1, (478, 3)) * [60, 80, 30] + [320, 240, 0]
    basis = rng.normal(0, 3, (51, 478, 3))
    lm = neutral[None] + np.einsum("nk,kpc->npc", W, basis) + rng.normal(0, 0.2, (n, 478, 3))
    # paupières : œil gauche du sujet (points 385/387/373/380) fermé sur 100-110
    for i in range(100, 111):
        for up, lo in ((385, 380), (387, 373)):
            lm[i, up, :2] = lm[i, lo, :2] = (lm[i, up, :2] + lm[i, lo, :2]) / 2
    mats = np.tile(np.eye(4), (n, 1, 1))
    for i in range(n):
        a = np.radians(12 * np.sin(t[i]))
        mats[i, :3, :3] = [[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]]
    np.savez_compressed(face.outputs(d)["raw"], fps=fps, size=np.array([640, 480]), t=t, weights=W,
                        names=np.array(names), mats=mats, lm=lm.astype(np.float32), ok=np.ones(n, bool))
    return W, names


def main():
    D = tempfile.mkdtemp(prefix="mocam_visage_")
    fd = os.path.join(D, "visage_001")
    video = sys.argv[1] if len(sys.argv) > 1 else None
    if video:
        os.makedirs(fd)
        os.link(video, os.path.join(fd, "video" + os.path.splitext(video)[1])) if os.name != "nt" else \
            __import__("shutil").copy(video, fd)
    else:
        Wsrc, names = fake_take(fd)
        face.write_meta(fd, tete=True, miroir=True)     # anciens réglages : ignorés désormais
    import bpy  # noqa: F401  (Blender en module)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "blender_addon"))
    import mocapcam_blender as mb
    from mocapcam import fbx as fbxmod
    # Blender en module plutôt qu'en sous-processus
    fbxmod.face_export = lambda j, f, g=None, blender=None, log=print: mb.face_file(j, f, g)
    t0 = time.time()
    out = face.run(fd, log=lambda *a: None)
    d = json.load(open(out["json"]))
    W = np.array(d["weights"])
    ok("courbes", W.shape[1] == 52 and np.isfinite(W).all() and W.min() >= 0 and W.max() <= 1,
       f"{W.shape[0]} images × {W.shape[1]} blendshapes en {time.time() - t0:.1f} s")
    if not video:
        bl = W[:, face.ARKIT.index("eyeBlinkLeft")]
        ok("paupière fermée détectée (œil gauche du sujet)", bl[102:109].min() > 0.8 and np.median(bl) < 0.3,
           f"fermé {bl[102:109].min():.2f}, médiane {np.median(bl):.2f}")
        H = np.array(d["head"])
        ok("tête toujours fixe", np.abs(H).max() == 0 and d.get("tete") is False,
           "la tête tourne de ±12° dans la vidéo, rien dans les courbes")
        sq = W[:, face.ARKIT.index("eyeSquintLeft")]
        ok("pas de plissement sur un œil fermé", sq[102:109].max() < 0.05, f"plissement max {sq[102:109].max():.2f}")
        j = names.index("jawOpen")
        r = np.corrcoef(Wsrc[:, j], W[:, face.ARKIT.index("jawOpen")])[0, 1]
        ok("lissage sans déformer la courbe", r > 0.97, f"corrélation avec la source {r:.3f}")
    # visage fixe : chaque blendshape ne bouge que sa zone (le reste du visage est immobile)
    dl = np.array(d["face"]["deltas"])
    moved = np.linalg.norm(dl, axis=2) > 1e-5
    eye = max(moved[face.ARKIT.index(n)].mean() for n in face.ARKIT if n.startswith("eye"))
    allm = max(moved[j].mean() for j in range(52))
    ok("blendshapes limitées à leur zone (tête fixe)", eye < 0.3 and not moved[51].any(),
       f"une forme des yeux bouge au plus {eye * 100:.0f} % des sommets, toute forme au plus {allm * 100:.0f} %")
    # yeux : formes géométriques (paupière haute posée sur la basse), regard sans déformation
    jb = face.ARKIT.index("eyeBlinkLeft")
    lid = face.LIDS["Left"]
    nv = np.array(d["face"]["vertices"])
    up, lo = nv[lid["up"]] + dl[jb][lid["up"]], nv[lid["lo"]] + dl[jb][lid["lo"]]
    gap0 = np.linalg.norm(nv[lid["up"]] - nv[lid["lo"]], axis=1).mean()
    look = max(np.abs(dl[face.ARKIT.index(n)]).max() for n in face.ARKIT if n.startswith("eyeLook"))
    ok("clignement : paupières jointes, sans pli", np.linalg.norm(up - lo, axis=1).max() < 0.01 * gap0 + 1e-9
       and moved[jb].sum() <= 40 and look == 0,
       f"{moved[jb].sum()} sommets bougent, écart restant {np.linalg.norm(up - lo, axis=1).max() * 1000:.2f} mm, regard : 0")
    root = sorted(os.listdir(fd))
    ok("sorties : FBX seulement (pas de CSV)", not any(f.endswith((".csv", ".json")) for f in root if f != "take.json")
       and os.path.exists(out["fbx"]), ", ".join(root))
    # FBX : 52 blendshapes animées
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=out["fbx"])
    me = next(o for o in bpy.data.objects if o.type == "MESH" and o.data.shape_keys)
    ks = me.data.shape_keys
    names_fbx = [k.name for k in ks.key_blocks[1:]]
    animated = bool(ks.animation_data and ks.animation_data.action)
    ok("FBX : visage + 52 blendshapes ARKit animées", names_fbx == face.ARKIT and animated,
       f"{len(me.data.vertices)} sommets, {len(names_fbx)} formes, animation {'oui' if animated else 'non'}")
    # vidéo filmée téléphone vertical mais enregistrée couchée : redressée au traitement
    if os.path.exists(face.MODEL_PATH) and video:
        from mocapcam.video import ffmpeg_exe
        import subprocess
        fd2 = os.path.join(D, "visages", "visage_couche")
        os.makedirs(fd2, exist_ok=True)
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", video, "-t", "4", "-vf",
                        "transpose=2,scale=640:-2", "-c:v", "libx264", "-preset", "veryfast",
                        os.path.join(fd2, "visage_couche.mp4")], check=True)
        face.run(fd2, log=lambda *a: None)
        o2 = face.outputs(fd2)
        cap = cv2.VideoCapture(o2["view"])
        vw, vh = cap.get(3), cap.get(4)
        cap.release()
        ok("vidéo couchée redressée", face.read_meta(fd2).get("rotation") == 90 and vh > vw,
           f"rotation {face.read_meta(fd2).get('rotation')}°, vidéo affichée {vw:.0f}×{vh:.0f}")
    # aperçu direct : une image du téléphone -> 52 valeurs + maillage (si le modèle est là)
    if os.path.exists(face.MODEL_PATH) and video:
        cap = cv2.VideoCapture(video)
        good, fr = cap.read()
        cap.release()
        live = face.LiveTracker(log=lambda *a: None)
        jpg = cv2.imencode(".jpg", cv2.resize(fr, None, fx=640 / max(fr.shape[:2]), fy=640 / max(fr.shape[:2])))[1].tobytes()
        r = live(jpg)
        ok("aperçu direct", r["found"] and len(r["w"]) == 52 and True,
           f"image {r['size'][0]}×{r['size'][1]}")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} tests réussis ; dossier : {D}")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
