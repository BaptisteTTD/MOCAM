"""Capture faciale avec une seule caméra : 52 blendshapes ARKit, tête fixe.

Suivi : MediaPipe Face Landmarker (478 points 3D, 51 blendshapes, pose de la tête) sur chaque
image de la vidéo. Puis, pour la qualité :
  * images sans visage comblées (interpolation), pics isolés retirés (médiane sur 3 images) ;
  * calibration : la valeur « au repos » de chaque blendshape (sa valeur basse sur la prise)
    est ramenée à 0, et l'amplitude étirée d'autant ;
  * lissage One Euro dans les deux sens (sans retard) : fort sur les sourcils et la tête,
    faible sur les paupières et la mâchoire (les clignements restent nets) ;
  * un visage MOCAM (maillage de 468 sommets) dont les 52 formes sont apprises sur la prise
    elle-même : il bouge comme l'acteur, pour l'aperçu et le FBX.

Sorties dans le dossier de la prise de visage :
  NOM.fbx   visage MOCAM animé (52 blendshapes ARKit, tête fixe)
  mocapcam/NOM.json  courbes + visage appris (aperçu du Studio)
"""
import json
import math
import os
import subprocess
import time
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
             "float16/latest/face_landmarker.task")
MODEL_PATH = os.path.join(ROOT, "modeles", "face_landmarker.task")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")

# les 52 blendshapes ARKit (ordre d'Apple) ; MediaPipe en donne 51 (pas tongueOut)
ARKIT = ["eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft", "eyeLookUpLeft",
         "eyeSquintLeft", "eyeWideLeft", "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight",
         "eyeLookOutRight", "eyeLookUpRight", "eyeSquintRight", "eyeWideRight", "jawForward",
         "jawRight", "jawLeft", "jawOpen", "mouthClose", "mouthFunnel", "mouthPucker", "mouthRight",
         "mouthLeft", "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
         "mouthDimpleLeft", "mouthDimpleRight", "mouthStretchLeft", "mouthStretchRight",
         "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper", "mouthPressLeft",
         "mouthPressRight", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft",
         "mouthUpperUpRight", "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft",
         "browOuterUpRight", "cheekPuff", "cheekSquintLeft", "cheekSquintRight", "noseSneerLeft",
         "noseSneerRight", "tongueOut"]

# réglages de lissage (fréquence de coupure de base en Hz, réactivité) par famille de formes
SMOOTH = {"faible": 1.6, "moyen": 1.0, "fort": 0.6}
FAMILY = [("eyeBlink", 4.5, 0.6), ("eyeLook", 2.5, 0.4), ("eye", 3.0, 0.4), ("jaw", 3.0, 0.5),
          ("mouth", 3.0, 0.5), ("brow", 1.8, 0.3), ("cheek", 2.0, 0.3), ("nose", 2.0, 0.3),
          ("tongue", 3.0, 0.5)]

# points du visage qui bougent peu avec les expressions : servent à retirer le mouvement de la tête
STABLE = [6, 168, 197, 195, 5, 4, 10, 151, 9, 8, 127, 356, 234, 454, 93, 323, 132, 361,
          33, 263, 133, 362, 168, 1]


def mirror_name(n):
    return n.replace("Left", "#").replace("Right", "Left").replace("#", "Right")


# ------------------------------------------------------------------------------------- modèle
def ensure_model(log=print):
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 1_000_000:
        return MODEL_PATH
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    log("téléchargement du modèle de visage MediaPipe (3,6 Mo)…")
    tmp = MODEL_PATH + ".part"
    urllib.request.urlretrieve(MODEL_URL, tmp)
    os.replace(tmp, MODEL_PATH)
    return MODEL_PATH


def _install_mediapipe(log=print):
    """MediaPipe s'installe tout seul au premier traitement de visage (pip, environnement MOCAM)."""
    import subprocess
    import sys
    log("installation de MediaPipe (une seule fois, environ 1 minute)…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "mediapipe"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("MediaPipe n'a pas pu s'installer : relancez installer.bat\n" + (r.stderr or "")[-400:])


def _mediapipe(log=print):
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        _install_mediapipe(log)
    try:
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import face_landmarker as fl
        from mediapipe.tasks.python.vision.core.vision_task_running_mode import VisionTaskRunningMode
        import mediapipe as mp
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("MediaPipe n'est pas installé : relancez installer.bat") from e
    return mp, BaseOptions, fl, VisionTaskRunningMode


# ------------------------------------------------------------------------------------- suivi
ROTATIONS = {90: "ROTATE_90_CLOCKWISE", 180: "ROTATE_180", 270: "ROTATE_90_COUNTERCLOCKWISE"}


def _rotate(frame, rot):
    import cv2
    return cv2.rotate(frame, getattr(cv2, ROTATIONS[rot])) if rot in ROTATIONS else frame


def detect_rotation(video_path, log=print, samples=9):
    """Rotation (degrés, sens horaire) qui met le visage à l'endroit : 0, 90, 180 ou 270.
    Une vidéo filmée téléphone vertical mais enregistrée « couchée » est ainsi redressée."""
    import cv2
    mp, BaseOptions, fl, Mode = _mediapipe(log)
    cap = cv2.VideoCapture(video_path)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames = []
    for k in range(samples):
        if n > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((k + 0.5) * n / samples))
        good, fr = cap.read()
        if good:
            frames.append(fr)
    cap.release()
    opts = fl.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=ensure_model(log)),
                                    running_mode=Mode.IMAGE, num_faces=1, min_face_detection_confidence=0.4)
    score = {r: 0 for r in (0, 90, 180, 270)}
    with fl.FaceLandmarker.create_from_options(opts) as det:
        for fr in frames:
            for rot in score:
                im = _rotate(fr, rot)
                r = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(im, cv2.COLOR_BGR2RGB)))
                if r.face_landmarks:
                    top, chin = r.face_landmarks[0][10], r.face_landmarks[0][152]
                    dy, dx = (chin.y - top.y) * im.shape[0], (chin.x - top.x) * im.shape[1]
                    if dy > abs(dx):                   # menton sous le front : visage à l'endroit
                        score[rot] += 1
    best = max(score, key=lambda r: (score[r], r == 0))
    return best if score[best] > score[0] else 0


def write_upright(video_path, out_path, rot, log=print):
    """Copie de la vidéo redressée (H.264, lisible dans MOCAM Studio)."""
    from .video import ffmpeg_exe
    vf = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}[rot]
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", video_path, "-vf", vf, "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
           out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
    if r.returncode != 0:
        log("    vidéo redressée non créée : " + (r.stderr or "")[-300:])
        return False
    return True


def _no_window():
    return dict(creationflags=0x08000000) if os.name == "nt" else {}


def track(video_path, log=print, progress=None, rotation=0):
    """Suivi image par image. Renvoie dict(fps, size, t, weights (N,51), names, mats (N,4,4),
    lm (N,478,3) en pixels, ok (N,)). `rotation` redresse les images avant le suivi."""
    import cv2
    mp, BaseOptions, fl, Mode = _mediapipe(log)
    model = ensure_model(log)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"vidéo illisible : {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if not (5 <= fps <= 240):
        fps = 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    opts = fl.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model), running_mode=Mode.VIDEO, num_faces=1,
        min_face_detection_confidence=0.4, min_face_presence_confidence=0.4, min_tracking_confidence=0.4,
        output_face_blendshapes=True, output_facial_transformation_matrixes=True)
    names, W, M, L, ok, T = None, [], [], [], [], []
    size = None
    with fl.FaceLandmarker.create_from_options(opts) as det:
        i = 0
        while True:
            good, frame = cap.read()
            if not good:
                break
            frame = _rotate(frame, rotation)
            h, w = frame.shape[:2]
            size = (w, h)
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            ts = int(round(i * 1000.0 / fps))
            r = det.detect_for_video(img, ts)
            if r.face_blendshapes:
                cats = [c for c in r.face_blendshapes[0] if c.category_name != "_neutral"]
                if names is None:
                    names = [c.category_name for c in cats]
                W.append([c.score for c in cats])
                M.append(np.array(r.facial_transformation_matrixes[0], float).reshape(4, 4))
                L.append([[p.x * w, p.y * h, p.z * w] for p in r.face_landmarks[0]])
                ok.append(True)
            else:
                W.append(None), M.append(None), L.append(None), ok.append(False)
            T.append(i / fps)
            i += 1
            if progress and i % 30 == 0:
                progress(i, total)
    cap.release()
    if names is None or sum(ok) < 5:
        raise RuntimeError("aucun visage trouvé dans la vidéo : cadrez le visage de face, bien éclairé")
    n = len(ok)
    nw, nl = len(names), max(len(x) for x in L if x is not None)
    Wa = np.full((n, nw), np.nan)
    Ma = np.full((n, 4, 4), np.nan)
    La = np.full((n, nl, 3), np.nan)
    for k in range(n):
        if ok[k]:
            Wa[k], Ma[k], La[k] = W[k], M[k], L[k]
    return dict(fps=float(fps), size=size, t=np.array(T), weights=Wa, names=names, mats=Ma, lm=La,
                ok=np.array(ok))


# ------------------------------------------------------------------------------------- filtres
def _fill(x):
    """Comble les NaN par interpolation (bords : valeur la plus proche). x : (N, …)."""
    x = np.array(x, float)
    shp = x.shape
    f = x.reshape(shp[0], -1)
    idx = np.arange(shp[0])
    for j in range(f.shape[1]):
        c = f[:, j]
        bad = np.isnan(c)
        if bad.all():
            c[:] = 0
        elif bad.any():
            c[bad] = np.interp(idx[bad], idx[~bad], c[~bad])
    return f.reshape(shp)


def _median3(x):
    if len(x) < 3:
        return x
    y = x.copy()
    y[1:-1] = np.median(np.stack([x[:-2], x[1:-1], x[2:]]), axis=0)
    return y


def _one_euro(x, fps, mincut, beta, dcut=1.0):
    """Filtre One Euro sur un signal 1D."""
    def alpha(fc):
        tau = 1.0 / (2 * math.pi * fc)
        return 1.0 / (1.0 + tau * fps)
    y = np.empty_like(x)
    y[0], dx = x[0], 0.0
    ad = alpha(dcut)
    for i in range(1, len(x)):
        d = (x[i] - y[i - 1]) * fps
        dx = dx + ad * (d - dx)
        a = alpha(mincut + beta * abs(dx))
        y[i] = y[i - 1] + a * (x[i] - y[i - 1])
    return y


def smooth(x, fps, mincut, beta):
    """One Euro aller et retour, moyenné : lisse sans décaler dans le temps."""
    f = _one_euro(x, fps, mincut, beta)
    b = _one_euro(x[::-1], fps, mincut, beta)[::-1]
    return 0.5 * (f + b)


def _family(name):
    for pre, fc, beta in FAMILY:
        if name.startswith(pre):
            return fc, beta
    return 2.5, 0.4


# ------------------------------------------------------------------------------------- tête
def _rot_to_euler(R):
    """Lacet (yaw, autour de l'axe vertical), tangage (pitch), roulis (roll), en degrés,
    repère caméra : x à droite de l'image, y en haut, z vers la caméra."""
    yaw = math.degrees(math.atan2(R[0, 2], R[2, 2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -R[1, 2]))))
    roll = math.degrees(math.atan2(R[1, 0], R[1, 1]))
    return yaw, pitch, roll


def _nearest_rot(R):
    u, _, vt = np.linalg.svd(R)
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u @ vt
    return r


def head_motion(mats, fps, strength):
    """Rotations de la tête relatives à sa pose moyenne (degrés) et déplacement (cm)."""
    R = np.array([_nearest_rot(m[:3, :3]) if np.isfinite(m).all() else np.full((3, 3), np.nan) for m in mats])
    good = np.isfinite(R).all(axis=(1, 2))
    R0 = _nearest_rot(np.nanmean(R[good], axis=0))
    E = np.full((len(R), 3), np.nan)
    for i in np.where(good)[0]:
        E[i] = _rot_to_euler(R0.T @ R[i])
    E = _fill(E)
    tr = _fill(np.array([m[:3, 3] if np.isfinite(m).all() else [np.nan] * 3 for m in mats]))
    tr = tr - np.median(tr, axis=0)
    for j in range(3):
        E[:, j] = smooth(_median3(E[:, j]), fps, 1.2 * strength, 0.05)
        tr[:, j] = smooth(_median3(tr[:, j]), fps, 1.2 * strength, 0.05)
    return E, tr


# ------------------------------------------------------------------------------------- paupières
EYE_PTS = {"Left": [362, 385, 387, 263, 373, 380], "Right": [33, 160, 158, 133, 153, 144]}
GAIN = {"jawOpen": 1.2}            # MediaPipe sous-estime un peu l'ouverture de la mâchoire


def _ear(lm, idx):
    """Ouverture de l'œil (eye aspect ratio) : hauteur des paupières / largeur de l'œil."""
    p = [lm[:, i, :2] for i in idx]
    w = np.linalg.norm(p[0] - p[3], axis=1)
    return (np.linalg.norm(p[1] - p[5], axis=1) + np.linalg.norm(p[2] - p[4], axis=1)) / (2 * np.maximum(w, 1e-6))


def eyelids(lm, miroir=False):
    """Fermeture des paupières (0 ouvert, 1 fermé) mesurée sur les points du visage. MediaPipe
    plafonne vers 0,6 pour un œil fermé : la mesure géométrique, calée sur la prise, va de 0 à 1."""
    out = {}
    for side, idx in EYE_PTS.items():
        e = _fill(_ear(lm, idx))
        opened = float(np.percentile(e, 80))
        closed = float(np.percentile(e, 1))
        if closed > 0.45 * opened:           # aucun clignement net dans la prise : repère typique
            closed = 0.2 * opened
        sub = side if not miroir else ("Right" if side == "Left" else "Left")
        v = np.clip((opened - e) / max(1e-6, opened - closed), 0, 1)
        out[sub] = np.clip((v - 0.1) / 0.9, 0, 1)            # œil ouvert : 0 franc
    return out


# ------------------------------------------------------------------------------------- calibration
def process(raw, lissage="moyen", intensite=1.0, miroir=False):
    """Courbes finales (N,52) dans l'ordre ARKIT, tête (N,3) en degrés, déplacement (N,3) en cm."""
    fps = raw["fps"]
    strength = SMOOTH.get(lissage, 1.0)
    src = _fill(raw["weights"])
    names = list(raw["names"])
    if miroir:
        names = [mirror_name(n) for n in names]
    out = np.zeros((len(src), len(ARKIT)))
    rest = {}
    lids = eyelids(raw["lm"], miroir) if raw.get("lm") is not None else {}
    for j, n in enumerate(names):
        if n not in ARKIT:
            continue
        c = _median3(np.clip(src[:, j], 0, 1))
        b = float(min(np.percentile(c, 5), 0.45))           # valeur au repos de cette forme
        if n.startswith("eyeLook"):
            b = min(b, 0.15)                                  # le regard n'a pas de « repos » sûr
        c = np.clip((c - b) / max(1e-3, 1 - b) * GAIN.get(n, 1.0), 0, 1)
        if n.startswith("eyeBlink") and n[8:] in lids:
            # paupières : mesure géométrique (0 à 1) mêlée à MediaPipe ramené à la même échelle
            c = np.clip(0.6 * lids[n[8:]] + 0.4 * np.clip(c / 0.62, 0, 1), 0, 1)
            c = np.clip(c / 0.88, 0, 1)                         # œil fermé : paupières jointes
        fc, beta = _family(n)
        c = smooth(c, fps, fc * strength, beta)
        out[:, ARKIT.index(n)] = np.clip(c * intensite, 0, 1)
        rest[n] = round(b, 3)
    # œil fermé : MediaPipe y voit aussi un plissement et un regard vers le bas ; on les retire
    # pour que les paupières ne se chevauchent pas
    for s in ("Left", "Right"):
        b = out[:, ARKIT.index("eyeBlink" + s)]
        out[:, ARKIT.index("eyeSquint" + s)] *= np.clip(1 - 1.2 * b, 0, 1)
        out[:, ARKIT.index("eyeWide" + s)] *= np.clip(1 - 1.5 * b, 0, 1)
    head, move = head_motion(raw["mats"], fps, strength)
    if miroir:
        head[:, 0] *= -1
        head[:, 2] *= -1
        move[:, 0] *= -1
    return out, head, move * 1.0, rest   # MediaPipe donne la translation en cm


# ------------------------------------------------------------------------------------- visage appris
def _triangles():
    """Triangles du maillage de visage MediaPipe (la liste d'arêtes va par triangles : a-b, b-c, c-a)."""
    from mediapipe.tasks.python.vision.face_landmarker import FaceLandmarksConnections as C
    e = C.FACE_LANDMARKS_TESSELATION
    return [[e[i].start, e[i + 1].start, e[i + 2].start] for i in range(0, len(e) - 2, 3)]


def _align(P, Q, idx):
    """Similitude qui amène P sur Q (moindres carrés sur les points idx). Renvoie P transformé."""
    a, b = P[idx], Q[idx]
    ma, mb = a.mean(0), b.mean(0)
    A, B = a - ma, b - mb
    u, s, vt = np.linalg.svd(A.T @ B)
    d = np.sign(np.linalg.det(u @ vt))
    D = np.diag([1, 1, d])
    R = u @ D @ vt
    sc = (s * np.diag(D)).sum() / (A ** 2).sum()
    return (P - ma) @ R * sc + mb


# paupières : contours appariés de l'angle externe à l'angle interne (côtés du sujet)
LIDS = {
    "Right": dict(up=[246, 161, 160, 159, 158, 157, 173], lo=[7, 163, 144, 145, 153, 154, 155],
                  up2=[247, 30, 29, 27, 28, 56, 190], lo2=[25, 110, 24, 23, 22, 26, 112],
                  crease=[113, 225, 224, 223, 222, 221, 189]),
    "Left": dict(up=[466, 388, 387, 386, 385, 384, 398], lo=[249, 390, 373, 374, 380, 381, 382],
                 up2=[467, 260, 259, 257, 258, 286, 414], lo2=[255, 339, 254, 253, 252, 256, 341],
                 crease=[342, 445, 444, 443, 442, 441, 413]),
}
EYE_GEOM = ("eyeBlink", "eyeWide", "eyeSquint", "eyeLook")


def eye_shapes(neutral, miroir=False):
    """Formes des yeux construites sur la géométrie des paupières (et non apprises) : la paupière
    supérieure descend sur l'inférieure sans plis, la peau autour suit en douceur. Les formes du
    regard (eyeLook) ne déforment pas le visage : il n'a pas de globes oculaires."""
    out = {}
    for side, L in LIDS.items():
        s = side if not miroir else ("Left" if side == "Right" else "Right")
        up, lo = np.array(L["up"]), np.array(L["lo"])
        gap = neutral[lo] - neutral[up]                    # de la paupière haute vers la basse
        blink, wide, squint = (np.zeros_like(neutral) for _ in range(3))
        blink[up] = 0.88 * gap
        blink[L["up2"]] = 0.62 * gap
        blink[L["crease"]] = 0.18 * gap
        blink[lo] = -0.12 * gap
        blink[L["lo2"]] = -0.05 * gap
        wide[up] = -0.30 * gap
        wide[L["up2"]] = -0.22 * gap
        wide[L["crease"]] = -0.10 * gap
        squint[lo] = -0.32 * gap
        squint[L["lo2"]] = -0.20 * gap
        squint[up] = 0.06 * gap
        out["eyeBlink" + s], out["eyeWide" + s], out["eyeSquint" + s] = blink, wide, squint
        for d in ("Down", "In", "Out", "Up"):
            out[f"eyeLook{d}{s}"] = np.zeros_like(neutral)
    return out


def learn_face(raw, weights, miroir=False):
    """Visage MOCAM : sommets au repos (468, 3) en mètres (repère Blender : x vers la gauche du
    sujet, y vers l'arrière, z en haut) et 52 déformations, apprises par régression sur la prise."""
    lm = raw["lm"][:, :468].copy()
    good = np.isfinite(lm).all(axis=(1, 2))
    lm = lm[good]
    Wt = weights[good]
    if len(lm) < 10:
        raise RuntimeError("trop peu d'images avec un visage pour construire le visage MOCAM")
    # pixels -> repère « face » : x à droite de l'image, y en haut, z vers la caméra
    P = np.stack([lm[..., 0], -lm[..., 1], -lm[..., 2]], -1)
    calm = np.argsort(Wt.sum(1))[:max(5, len(Wt) // 20)]  # images les plus neutres
    ref = np.median(P[calm], axis=0)
    for _ in range(2):
        A = np.array([_align(p, ref, STABLE) for p in P])
        ref = np.median(A[calm], axis=0)
    # échelle : du haut du front (10) au menton (152) ≈ 19 cm
    sc = 0.19 / max(1e-6, np.linalg.norm(ref[10] - ref[152]))
    A = (A - ref[[1]]) * sc                                # origine au bout du nez
    neutral = (ref - ref[[1]]) * sc
    # yeux : formes géométriques, retirées des mouvements observés avant d'apprendre les autres
    eyes = eye_shapes(neutral, miroir)
    geo = [j for j, n in enumerate(ARKIT) if n in eyes]
    rest = [j for j, n in enumerate(ARKIT) if n not in eyes]
    E = np.stack([eyes[ARKIT[j]] for j in geo]).reshape(len(geo), -1)
    D = (A - neutral).reshape(len(A), -1) - Wt[:, geo] @ E
    X = Wt[:, rest]
    lam = 1e-2 * max(1e-6, float(np.mean(np.diag(X.T @ X)))) + 1e-4 * len(X)
    delta = np.zeros((len(ARKIT), 468 * 3))
    delta[rest] = np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ D)
    delta = localize(delta.reshape(len(ARKIT), 468, 3), neutral, miroir)
    for j in geo:
        delta[j] = eyes[ARKIT[j]]
    # repère Blender (le sujet regarde vers -Y) : x image -> +X (gauche du sujet), haut -> +Z,
    # vers la caméra -> -Y ; vidéo en miroir : x inversé
    def to_b(v):
        x = -v[..., 0] if miroir else v[..., 0]
        return np.stack([x, -v[..., 2], v[..., 1]], -1)
    nb = to_b(neutral)
    nb[:, 2] -= nb[:, 2].min()                             # menton à z = 0 (posé sur l'os de la tête)
    return nb, to_b(delta), _triangles()


def _region_pts():
    from mediapipe.tasks.python.vision.face_landmarker import FaceLandmarksConnections as C
    pts = lambda conns: sorted({p for c in conns for p in (c.start, c.end)})
    return dict(eyeL=pts(C.FACE_LANDMARKS_LEFT_EYE), eyeR=pts(C.FACE_LANDMARKS_RIGHT_EYE),
                browL=pts(C.FACE_LANDMARKS_LEFT_EYEBROW), browR=pts(C.FACE_LANDMARKS_RIGHT_EYEBROW),
                lips=pts(C.FACE_LANDMARKS_LIPS), nose=pts(C.FACE_LANDMARKS_NOSE))


def _smoothstep(d, r, fall):
    t = np.clip((d - r) / fall, 0, 1)
    return 1 - t * t * (3 - 2 * t)


def shape_masks(neutral, miroir=False):
    """Zone d'action de chaque blendshape (0 à 1 par sommet, bords adoucis) : un clignement ne
    touche que la paupière, un sourire que le bas du visage, etc. neutral : repère « face »."""
    R = _region_pts()
    if miroir:      # vidéo en miroir : l'œil gauche du sujet est l'œil « droit » de MediaPipe
        R["eyeL"], R["eyeR"], R["browL"], R["browR"] = R["eyeR"], R["eyeL"], R["browR"], R["browL"]
    V = neutral
    dist = {k: np.min(np.linalg.norm(V[:, None] - V[None, idx], axis=2), axis=1) for k, idx in R.items()}
    near = lambda k, r, fall=0.02: _smoothstep(dist[k], r, fall)
    # bas du visage (bouche, mâchoire) : sous la base du nez, fondu de 2 cm au-dessus
    base = V[2, 1]
    lower = _smoothstep(V[:, 1] - (base - 0.008), 0.0, 0.02)
    M = {}
    for n in ARKIT:
        side = "L" if n.endswith("Left") else "R" if n.endswith("Right") else None
        if n.startswith(("eyeBlink", "eyeLook", "eyeSquint", "eyeWide")):
            m = near("eye" + side, 0.012, 0.015)
        elif n.startswith("browInnerUp"):
            m = np.maximum(near("browL", 0.02), near("browR", 0.02)) * _smoothstep(np.abs(V[:, 0]), 0.03, 0.03)
        elif n.startswith("brow"):
            m = near("brow" + side, 0.02)
        elif n.startswith("cheekSquint"):
            m = near("eye" + side, 0.03) * _smoothstep(V[:, 1] - (V[R["eye" + side], 1].min() + 0.005), 0, 0.01)
        elif n.startswith("noseSneer"):
            m = near("nose", 0.015)
        elif n == "cheekPuff":
            m = np.maximum(near("lips", 0.04), lower * 0.0)
        elif n.startswith(("jaw", "mouth")):
            m = lower
        else:                                # tongueOut : pas de langue sur ce visage
            m = np.zeros(len(V))
        M[n] = m
    return M


def localize(delta, neutral, miroir=False):
    """Retire de chaque blendshape tout mouvement d'ensemble de la tête (translation, rotation,
    échelle, appris par erreur quand la tête bouge en même temps qu'une expression), puis la
    limite à sa zone : le visage reste fixe, seules les parties concernées bougent."""
    M = shape_masks(neutral, miroir)
    out = np.zeros_like(delta)
    P = neutral - neutral.mean(0)
    for j, n in enumerate(ARKIT):
        d, m = delta[j], M[n]
        w = 1.0 - m                                         # points qui ne doivent pas bouger
        if w.sum() > 10:
            # d ≈ t + ω × p + s p sur ces points (petits mouvements) : moindres carrés pondérés
            A = np.zeros((len(P), 3, 7))
            A[:, :, :3] = np.eye(3)
            x, y, z = P[:, 0], P[:, 1], P[:, 2]
            A[:, 0, 3:6] = np.stack([np.zeros_like(x), z, -y], 1)
            A[:, 1, 3:6] = np.stack([-z, np.zeros_like(x), x], 1)
            A[:, 2, 3:6] = np.stack([y, -x, np.zeros_like(x)], 1)
            A[:, :, 6] = P
            sw = np.sqrt(w)[:, None, None]
            sol, *_ = np.linalg.lstsq((A * sw).reshape(-1, 7), (d * sw[..., 0]).reshape(-1), rcond=None)
            d = d - (A @ sol)
        out[j] = d * m[:, None]
    return out


# ------------------------------------------------------------------------------------- sorties
def face_dir_take(face_dir):
    return os.path.basename(os.path.normpath(face_dir))


def find_video(face_dir):
    for fn in sorted(os.listdir(face_dir)):
        if fn.lower().endswith(VIDEO_EXTS) and not fn.startswith("."):
            return os.path.join(face_dir, fn)
    return None


def outputs(face_dir):
    n = face_dir_take(face_dir)
    return dict(json=os.path.join(face_dir, "mocapcam", f"{n}.json"),
                fbx=os.path.join(face_dir, f"{n}.fbx"), glb=os.path.join(face_dir, "mocapcam", f"{n}.glb"),
                raw=os.path.join(face_dir, "mocapcam", f"{n}_suivi.npz"),
                view=os.path.join(face_dir, "mocapcam", f"{n}_vue.mp4"))


def curves_path(face_dir):
    """Courbes et visage appris (prises traitées avant la 1.7 : à la racine du dossier)."""
    p = outputs(face_dir)["json"]
    old = os.path.join(face_dir, f"{face_dir_take(face_dir)}.json")
    return p if os.path.exists(p) or not os.path.exists(old) else old


def read_meta(face_dir):
    p = os.path.join(face_dir, "take.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_meta(face_dir, **kw):
    m = read_meta(face_dir)
    m.update(kw)
    with open(os.path.join(face_dir, "take.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    return m


def run(face_dir, studio_dir=None, blender=None, log=print, retrack=False):
    """Traitement complet d'une prise de visage (étapes « N/3 » pour MOCAM Studio). La tête reste
    fixe et la vidéo est lue telle quelle (non inversée) : seules les expressions sont animées."""
    from . import fbx as fbxmod
    out = outputs(face_dir)
    meta = read_meta(face_dir)
    lissage = meta.get("lissage", "moyen")
    intensite = float(meta.get("intensite", 100)) / 100.0
    miroir = False
    video = find_video(face_dir)
    if not video:
        raise RuntimeError("pas de vidéo dans ce dossier")
    os.makedirs(os.path.dirname(out["raw"]), exist_ok=True)
    log("1/3 suivi du visage, image par image")
    if (retrack or "rotation" not in meta or not os.path.exists(out["raw"])
            or os.path.getmtime(out["raw"]) < os.path.getmtime(video)):
        last = [0.0]
        rot = detect_rotation(video, log)
        meta["rotation"] = rot
        write_meta(face_dir, rotation=rot)
        if os.path.exists(out["view"]):
            os.remove(out["view"])
        if rot:
            log(f"    vidéo couchée : redressée de {rot}° pour la voir à l'endroit")
            write_upright(video, out["view"], rot, log)

        def prog(i, n):
            if time.time() - last[0] > 2:
                last[0] = time.time()
                log(f"    {i} images" + (f" sur {n}" if n else ""))
        raw = track(video, log, prog, rot)
        np.savez_compressed(out["raw"], fps=raw["fps"], size=np.array(raw["size"]), t=raw["t"],
                            weights=raw["weights"], names=np.array(raw["names"]), mats=raw["mats"],
                            lm=raw["lm"].astype(np.float32), ok=raw["ok"])
    else:
        z = np.load(out["raw"])
        raw = dict(fps=float(z["fps"]), size=tuple(z["size"]), t=z["t"], weights=z["weights"],
                   names=[str(x) for x in z["names"]], mats=z["mats"], lm=z["lm"].astype(float), ok=z["ok"])
        log("    suivi déjà fait (gardé)")
    n = len(raw["t"])
    found = int(raw["ok"].sum())
    log(f"    visage trouvé sur {found} images sur {n} ({100 * found / max(1, n):.0f} %) à {raw['fps']:.0f} i/s")
    log(f"2/3 calibration et lissage ({lissage}, intensité {intensite * 100:.0f} %)")
    W, head, move, rest = process(raw, lissage, intensite, miroir)
    head = np.zeros_like(head)               # tête toujours fixe
    move = np.zeros_like(move)
    neutral, delta, tris = learn_face(raw, W, miroir)
    data = dict(fps=raw["fps"], frames=n, names=ARKIT, weights=np.round(W, 4).tolist(),
                head=np.round(head, 3).tolist(), move_cm=np.round(move, 3).tolist(), rest=rest,
                found=found, lissage=lissage, intensite=round(intensite * 100), tete=False,
                face=dict(vertices=np.round(neutral, 5).tolist(), triangles=tris,
                          deltas=np.round(delta, 5).tolist()))
    for old in (f"{face_dir_take(face_dir)}.json", f"{face_dir_take(face_dir)}.csv"):   # anciennes versions
        if os.path.exists(os.path.join(face_dir, old)):
            os.remove(os.path.join(face_dir, old))
    with open(out["json"], "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    peak = sorted(((float(W[:, i].max()), nm) for i, nm in enumerate(ARKIT)), reverse=True)[:5]
    log("    formes les plus actives : " + ", ".join(f"{nm} {v * 100:.0f} %" for v, nm in peak))
    log("3/3 FBX du visage MOCAM (52 blendshapes ARKit)")
    res = fbxmod.face_export(out["json"], out["fbx"], out["glb"], blender=blender, log=log)
    if not res:
        log("    FBX non créé : Blender est introuvable")
    write_meta(face_dir, processed=time.strftime("%Y-%m-%d %H:%M:%S"), frames=n, fps=raw["fps"])
    log("Terminé")
    return out


# ------------------------------------------------------------------------------------- aperçu direct
class LiveTracker:
    """Suivi image par image de l'aperçu envoyé par le téléphone (MOCAM Studio, onglet Visage) :
    les 52 valeurs ARKit."""

    def __init__(self, log=print):
        self.log = log
        self.det = None
        self.t0 = time.monotonic()

    def reset(self):
        pass

    def _open(self):
        mp, BaseOptions, fl, Mode = _mediapipe(self.log)
        opts = fl.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=ensure_model(self.log)), running_mode=Mode.VIDEO,
            num_faces=1, min_face_detection_confidence=0.4, min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4, output_face_blendshapes=True)
        self.mp = mp
        self.det = fl.FaceLandmarker.create_from_options(opts)

    def __call__(self, jpg):
        import cv2
        if self.det is None:
            self._open()
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        r = self.det.detect_for_video(self.mp.Image(image_format=self.mp.ImageFormat.SRGB,
                                                    data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB)),
                                      int((time.monotonic() - self.t0) * 1000))
        if not r.face_blendshapes:
            return dict(found=False, size=[w, h])
        sc = {c.category_name: c.score for c in r.face_blendshapes[0]}
        wts = [float(np.clip(sc.get(n, 0.0), 0, 1)) for n in ARKIT]
        for s in ("Left", "Right"):            # MediaPipe plafonne vers 0,6 pour un œil fermé
            j = ARKIT.index("eyeBlink" + s)
            wts[j] = float(np.clip((wts[j] - 0.1) / 0.5, 0, 1))
        return dict(found=True, size=[w, h], w=[round(x, 3) for x in wts])

