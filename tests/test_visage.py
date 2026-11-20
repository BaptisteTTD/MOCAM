"""Capture faciale : calibration, visage appris, FBX à 52 blendshapes, transfert sur personnage.

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
        face.write_meta(fd, tete=True)
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
        ok("rotation de la tête", 18 < H[:, 0].max() - H[:, 0].min() < 30, f"lacet de {H[:, 0].min():.1f}° à {H[:, 0].max():.1f}°")
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
    face.write_meta(fd, tete=False)
    out = face.run(fd, log=lambda *a: None)
    H0 = np.array(json.load(open(out["json"]))["head"])
    ok("tête fixe par défaut", np.abs(H0).max() == 0, "rotation désactivée : tête immobile")
    # FBX : 52 blendshapes animées
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=out["fbx"])
    me = next(o for o in bpy.data.objects if o.type == "MESH" and o.data.shape_keys)
    ks = me.data.shape_keys
    names_fbx = [k.name for k in ks.key_blocks[1:]]
    animated = bool(ks.animation_data and ks.animation_data.action)
    ok("FBX : visage + 52 blendshapes ARKit animées", names_fbx == face.ARKIT and animated,
       f"{len(me.data.vertices)} sommets, {len(names_fbx)} formes, animation {'oui' if animated else 'non'}")
    # transfert sur un personnage (ici : le visage MOCAM lui-même, renommé façon CC4 « Eye_Blink_L »)
    char = os.path.join(D, "tete.fbx")
    for k in ks.key_blocks[1:]:
        k.name = {"eyeBlinkLeft": "Eye_Blink_L", "jawOpen": "A25_Jaw_Open"}.get(k.name, k.name)
    if ks.animation_data:
        ks.animation_data_clear()
    bpy.ops.export_scene.fbx(filepath=char, use_selection=False)
    rep = mb.face_character_file(out["json"], char, os.path.join(D, "tete_anime.fbx"))
    ok("transfert sur les blendshapes d'un personnage", rep["shapes"] == 52 and not rep["missing"],
       f"{rep['shapes']} formes reliées (noms Eye_Blink_L, A25_Jaw_Open reconnus)")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} tests réussis ; dossier : {D}")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
