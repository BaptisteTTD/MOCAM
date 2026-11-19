"""Enchaînement complet d'une prise."""
import itertools
import json
import os
import re
import shutil
import time
import unicodedata

import cv2
import numpy as np

from . import autocalib, fbx, intrinsics, markers as mk, motion_opt, pose2d, sync, syncbody, triangulate, video
from .skeleton import INDEX, JOINT_NAMES

DEFAULT_STUDIO = {
    "cameras": ["iphone", "poco"],
    "fps": 60,
    "max_height": 1080,
    "marker_height": 0.01,
    "markers": [
        {"name": "A", "color": "rouge", "position": [0.0, 0.0, 0.0]},
        {"name": "B", "color": "vert", "position": [2.5, 0.0, 0.0]},
        {"name": "C", "color": "bleu", "position": [2.5, 2.5, 0.0]},
        {"name": "D", "color": "jaune", "position": [0.0, 2.5, 0.0]},
        {"name": "E", "color": "rouge", "position": [0.0, 1.25, 0.0]},
    ],
    "volume": {"margin": 0.5, "height": 2.3},
    # taille de l'acteur (cm) : le squelette exporté est mis à cette taille (vide = échelle des boules)
    "actor_height_cm": None,
    "character": "",        # personnage animé après chaque traitement (studio/personnages/<id>) ; vide : le fourni (Y Bot)
    "blender": "",          # chemin de blender.exe pour l'export FBX (vide = recherche automatique)
    "fbx": True,            # créer <prise>.fbx (squelette Mixamo animé) après le traitement
    "sync_by_motion": True,  # affiner la synchro du bip avec le mouvement de l'acteur
    "pose": {"mode": "performance", "device": "cpu", "backend": "onnxruntime"},
    "reconstruction": {"score_body": 0.3, "score_hands": 0.3, "score_face": 0.5,
                       "max_reproj_px": 25.0},
    "filtering": {"max_jump_m": 0.15, "max_gap_s": 0.3,
                  "cutoff_body_hz": 6.0, "cutoff_hands_hz": 8.0},
    # affinage global du mouvement (os de longueur constante, fluidité, pieds posés)
    "refine": {"enabled": True, "smooth_body": 2.0, "smooth_hands": 8.0, "bone_weight": 2000.0,
               "contact_weight": 2500.0,
               "knee_weight": 2000.0, "knee_min_deg": 2.0, "knee_side_deg": 30.0, "tie_weight": 3000.0},
}


def load_studio(studio_dir):
    path = os.path.join(studio_dir, "studio.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_STUDIO.items():
        cfg.setdefault(k, v)
    h = cfg["marker_height"]
    for m in cfg["markers"]:
        p = list(m["position"]) + [0.0] * (3 - len(m["position"]))
        m["position_3d"] = [p[0], p[1], p[2] + h]
    return cfg


def init_studio(studio_dir):
    os.makedirs(os.path.join(studio_dir, "intrinseques"), exist_ok=True)
    path = os.path.join(studio_dir, "studio.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STUDIO, f, indent=2, ensure_ascii=False)
    return path


def _markers_for_solver(cfg):
    return [dict(m, position=m["position_3d"]) for m in cfg["markers"]]


def _lens(meta, name):
    return ((meta or {}).get("cameras", {}).get(name, {}) or {}).get("lens") or "wide"


def _fov(meta, name):
    """Champ de vision choisi dans l'app (équivalent zoom), ou None (prises plus anciennes)."""
    c = ((meta or {}).get("cameras", {}).get(name, {}) or {})
    try:
        v = float(c.get("fov"))
        return v if 0.2 <= v <= 10 else None
    except (TypeError, ValueError):
        return None


def _evaluate_layout(views, ml):
    """Pose de chaque caméra pour une disposition donnée ; score total (plus bas = mieux)."""
    total, poses = 0.0, []
    for v in views:
        try:
            p = mk.solve_camera_pose(v["bg"], ml, v["K"], v["dist"], lens=v["lens"], cands=v["cands"],
                                     fov=v.get("fov"))
        except RuntimeError:
            p = None
        poses.append(p)
        total += p["score"] if p else 100.0
    return total, poses


def _layout_ok(poses, max_rms=8.0):
    # focale nominale (pas encore affinée) : quelques pixels d'écart restent normaux
    return all(p is not None and p["rms"] <= max_rms for p in poses)


FEMININE = {"vert": "verte", "bleu": "bleue", "violet": "violette"}


def _boule(m):
    return f"la boule {FEMININE.get(m['color'], m['color'])} ({m['name']})"


def choose_layout(views, mlist, log=print, swaps=True):
    """Disposition des boules qui explique le mieux toutes les caméras.

    * telle que décrite dans studio.json, ou son symétrique (B et C de l'autre côté de la
      ligne A-D : mêmes distances, seul le sens change) : pris en compte sans rien dire de plus ;
    * deux boules de couleurs différentes échangées : retenu seulement si c'est nettement
      mieux, avec un message pour l'utilisateur.
    Retourne (marqueurs, poses, description) ; poses[i] vaut None si la caméra i échoue."""
    base = []
    for mir in (False, True):
        ml = mk.mirrored(mlist) if mir else mlist
        sc, ps = _evaluate_layout(views, ml)
        base.append((sc, mir, ps, ml))
    base.sort(key=lambda r: (round(r[0], 2), r[1]))
    found = []
    for i, j in itertools.combinations(range(len(mlist)) if swaps else [], 2):
        if mlist[i]["color"] == mlist[j]["color"]:
            continue
        for mir in (False, True):
            ml = mk.swapped(mlist, i, j)
            ml = mk.mirrored(ml) if mir else ml
            sc, ps = _evaluate_layout(views, ml)
            found.append((sc, mir, ps, ml, (i, j)))
    found.sort(key=lambda r: (round(r[0], 2), r[1]))   # à égalité : l'explication sans miroir
    sc, mir, poses, ml = base[0]
    if found and _layout_ok(found[0][2]) and (not _layout_ok(poses) or found[0][0] < 0.7 * sc):
        sc, mir, poses, ml, (i, j) = found[0]
        a, b = mlist[i], mlist[j]
        log(f"  ATTENTION : {_boule(a)} et {_boule(b)} semblent échangées par rapport à studio.json. "
            "Le traitement en tient compte ; vous pouvez les laisser ainsi.")
        return ml, poses, f"{a['name']}<->{b['name']}" + (" miroir" if mir else "")
    if mir and _layout_ok(poses):
        log("  disposition des boules en miroir par rapport à studio.json (B et C de l'autre côté "
            "de la ligne A-D) : prise en compte automatiquement")
    return ml, poses, "miroir" if mir else "telle que décrite"


def calibrate_extrinsics(cfg, studio_dir, norm_videos, work, log=print, meta=None):
    mlist = _markers_for_solver(cfg)
    views = []
    for name, path in norm_videos.items():
        ipath = os.path.join(studio_dir, "intrinseques", f"{name}.json")
        info = video.video_info(path)
        bg = video.median_frame(path)
        cv2.imwrite(os.path.join(work, f"fond_{name}.jpg"), bg)
        fixed = os.path.exists(ipath)   # calibration optique au damier (optionnelle)
        if fixed:
            intr = intrinsics.scaled_to(intrinsics.load(ipath), info["width"], info["height"])
            K, dist = intr["K"], intr["dist"]
        else:                           # sans damier : focale de l'objectif, affinée ensuite
            K, dist = None, None
        frames = video.sample_frames(path, 30)
        views.append(dict(name=name, info=info, bg=bg, K=K, dist=dist, fixed=fixed, lens=_lens(meta, name),
                          fov=_fov(meta, name), cands=mk.find_candidates(bg, mlist, frames)))
    mlist, poses, layout = choose_layout(views, mlist, log)
    failed = [v["name"] for v, p in zip(views, poses) if p is None]
    if failed:
        raise RuntimeError(
            f"Boules introuvables ou incohérentes pour : {', '.join(failed)}. Chaque caméra doit voir au "
            "moins 4 boules, et leur disposition doit correspondre à studio.json (mêmes distances). "
            f"Regardez fond_*.jpg dans {work}.")
    # positions réellement utilisées (miroir / inversion) : reprises pour l'export
    by_name = {m["name"]: m for m in mlist}
    for m in cfg["markers"]:
        m["position_3d"] = list(by_name[m["name"]]["position"])
    vol = mk.volume_box(mlist, cfg["volume"]["margin"], cfg["volume"]["height"])
    cams = []
    for v, pose in zip(views, poses):
        name, info = v["name"], v["info"]
        dbg = mk.draw_debug(v["bg"], mlist, pose["K"], pose["dist"], pose, vol)
        cv2.imwrite(os.path.join(work, f"calibration_{name}.jpg"), dbg)
        miss = f" (non utilisées : {', '.join(pose['missing'])})" if pose["missing"] else ""
        if v["fixed"]:
            src = "damier"
        elif pose.get("aligned"):
            src = "approximative : boules vues presque alignées"
        else:
            src = "estimée par les boules"
        log(f"  {name} : erreur {pose['rms']:.2f} px, focale {pose['K'][0, 0]:.0f} px ({src}), "
            f"caméra à ({pose['center'][0]:.2f}, {pose['center'][1]:.2f}, {pose['center'][2]:.2f}) m{miss}")
        if pose["rms"] > 4:
            log(f"  ATTENTION {name} : erreur élevée, vérifiez calibration_{name}.jpg")
        f_nom = (v["K"] if v["K"] is not None else
                 mk.default_K(info["width"], info["height"], v["lens"], v.get("fov")))[0, 0]
        cams.append(dict(name=name, K=pose["K"], dist=np.asarray(pose["dist"], float), R=pose["R"],
                         t=np.asarray(pose["t"]), center=pose["center"], rms=pose["rms"],
                         width=info["width"], height=info["height"],
                         marker_obs=pose["marker_obs"], intrinsics_fixed=v["fixed"],
                         lens=v["lens"], f_nominal=float(f_nom)))
    return cams, vol


def actor_height(X):
    """Taille approximative de l'acteur (m) d'après la longueur médiane des jambes
    (hanche -> cheville ≈ 0,49 × la taille). Sert à repérer une erreur d'échelle :
    distances entre boules de studio.json différentes de la réalité."""
    J = {n: i for i, n in enumerate(JOINT_NAMES)}
    legs = []
    for side in ("l", "r"):
        a = np.linalg.norm(X[:, J[f"{side}_hip"]] - X[:, J[f"{side}_knee"]], axis=1)
        b = np.linalg.norm(X[:, J[f"{side}_knee"]] - X[:, J[f"{side}_ankle"]], axis=1)
        legs.append(a + b)
    leg = np.concatenate(legs)
    leg = leg[np.isfinite(leg)]
    if len(leg) < 10:
        return None
    return float(np.median(leg) / 0.49)


def _seg_median(X, a, b):
    v = np.linalg.norm(X[:, a] - X[:, b], axis=1)
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size > 10 else None


def rig_height(X):
    """Taille (m) du squelette Mixamo exporté, debout, du sol au sommet de la tête.

    Même règle que l'add-on Blender (create_mixamo_armature) : cuisses, tibias, buste et cou
    mesurés sur la prise, chevilles et crâne en proportion. Saisir la taille réelle de
    l'acteur met donc le squelette exporté exactement à cette taille."""
    def mid(a, b):
        return (X[:, INDEX[a]] + X[:, INDEX[b]]) / 2
    seg = {}
    for k, (a, b) in dict(thigh=("hip", "knee"), shin=("knee", "ankle")).items():
        v = [_seg_median(X, INDEX[f"{s}_{a}"], INDEX[f"{s}_{b}"]) for s in ("l", "r")]
        v = [x for x in v if x is not None]
        if not v:
            return None
        seg[k] = float(np.mean(v))
    Y = np.stack([mid("l_hip", "r_hip"), mid("l_shoulder", "r_shoulder"), mid("l_ear", "r_ear")], axis=1)
    torso, neck = _seg_median(Y, 0, 1), _seg_median(Y, 1, 2)
    if torso is None or neck is None:
        return None
    sc = (seg["thigh"] + seg["shin"]) / 0.85
    return seg["shin"] + seg["thigh"] + torso + neck + 0.24 * sc


def target_height_m(cfg, meta):
    """Taille indiquée pour l'acteur (take.json de la prise, sinon studio.json), en m."""
    for src in (meta or {}, cfg):
        v = src.get("actor_height_cm")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if 50 <= v <= 260:
            return v / 100.0
    return None


def scale_world(X, cams, vol, s):
    """Met toute la scène à l'échelle autour de l'origine (le sol reste le sol)."""
    X = X * s
    for c in cams:
        c["t"] = (np.asarray(c["t"], float) * s).reshape(np.shape(c["t"]))
        c["center"] = [float(v) * s for v in c["center"]]
    vol = (np.asarray(vol[0], float) * s, np.asarray(vol[1], float) * s)
    return X, cams, vol


def load_take_meta(take_dir):
    """Métadonnées écrites par MocapCam Studio (take.json), ou None pour une prise manuelle."""
    path = os.path.join(take_dir, "take.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_delays(norm_videos, manual=None, take_meta=None, log=print):
    names = list(norm_videos)
    manual = manual or {}
    audio = {n: video.read_audio(norm_videos[n]) for n in names}

    # 1) prise lancée par MocapCam Studio : bips joués par le PC (départ, et arrêt)
    chirps = (take_meta or {}).get("chirps_at_pc") or (
        [take_meta["chirp_at_pc"]] if take_meta and take_meta.get("chirp_at_pc") else [])
    if chirps and not manual:
        offs = [(c - chirps[0]) / 1000.0 for c in chirps]
        found = {}
        for n in names:
            peaks = sync.find_chirps(audio[n], max_n=len(chirps) + 2)
            found[n] = sync.match_chirps(peaks, offs)
            desc = ", ".join(f"{t:.3f} s" for _, t in sorted(found[n].items())) or "aucun"
            log(f"  {n} : bip(s) trouvé(s) à {desc}")
        common = set.intersection(*(set(f) for f in found.values())) if found else set()
        if common:
            ks = sorted(common)
            delays = []
            for n in names:
                d = [found[n][k] - found[names[0]][k] for k in ks]
                delays.append(float(np.mean(d)))
                if n != names[0]:
                    drift = (d[-1] - d[0]) * 1000 if len(d) > 1 else None
                    extra = f", dérive {drift:+.1f} ms entre les 2 bips" if drift is not None else ""
                    log(f"  {n} : décalage {delays[-1] * 1000:+.1f} ms par rapport à {names[0]}"
                        f" ({len(ks)} bip(s){extra})")
                    if drift is not None and abs(drift) > 20:
                        log("  ATTENTION : forte dérive entre les deux bips, vérifiez les cadences.")
            return delays
        cams = take_meta.get("cameras", {})
        st = {n: cams.get(n, {}).get("started_at_pc") for n in names}
        if all(v is not None for v in st.values()):
            log("  ATTENTION : bip peu audible, synchro par les horloges des téléphones "
                "(précision ~1 image). Montez le volume du PC.")
            return [-(st[n] - st[names[0]]) / 1000.0 for n in names]

    # 2) prise manuelle : clap au début
    delays = [0.0]
    for n in names[1:]:
        if n in manual:
            d, conf = float(manual[n]), None
        else:
            d, conf = sync.audio_delay(audio[names[0]], audio[n])
        delays.append(d)
        c = "manuel" if conf is None else f"confiance {conf:.1f}"
        log(f"  {n} : décalage {d * 1000:+.1f} ms par rapport à {names[0]} ({c})")
        if conf is not None and conf < 1.5:
            log("  ATTENTION : synchro audio peu fiable. Faites un clap net au début, "
                "ou indiquez le décalage avec --decalage.")
    return delays


def _jsonable(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def export_motion(path, X, fps, cams, cfg, vol, stats):
    pos = [[None if not np.isfinite(p).all() else [round(float(v), 4) for v in p]
            for p in frame] for frame in X]
    data = {
        "format": "mocapcam", "version": 1, "fps": fps, "units": "m", "up": "Z",
        "joint_names": JOINT_NAMES, "frame_count": int(X.shape[0]),
        # temps (s) de la première image, par rapport au début de la vidéo de référence
        "start_time_s": round(stats.get("first_frame", 0) / fps, 4),
        "positions": pos,
        "cameras": [dict(name=c["name"], K=np.asarray(c["K"]).tolist(),
                         dist=np.asarray(c["dist"]).ravel().tolist(), width=c["width"],
                         height=c["height"], R=np.asarray(c["R"]).tolist(),
                         t=np.asarray(c["t"]).tolist(), center=list(c["center"]))
                    for c in cams],
        "markers": [dict(name=m["name"], color=m["color"], position=m["position_3d"])
                    for m in cfg["markers"]],
        "volume": {"min": vol[0].tolist(), "max": vol[1].tolist()},
        "stats": stats,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), default=_jsonable)
    return path


# --------------------------------------------------------------------------- personnages riggés

def characters_dir(studio_dir):
    return os.path.join(studio_dir, "personnages")


def list_characters(studio_dir):
    d = characters_dir(studio_dir)
    out = []
    if os.path.isdir(d):
        for cid in sorted(os.listdir(d)):
            mf = os.path.join(d, cid, "personnage.json")
            if os.path.exists(mf):
                try:
                    with open(mf, encoding="utf-8") as f:
                        m = json.load(f)
                except (OSError, ValueError):
                    continue
                m["id"] = cid
                out.append(m)
    return out


def get_character(studio_dir, cid):
    return next((c for c in list_characters(studio_dir) if c["id"] == cid), None)


# personnages fournis avec le programme (ex. Y Bot de Mixamo) : fichiers posés dans ce dossier,
# installés tout seuls dans studio/personnages/ et animés par défaut. Ce dossier n'est pas
# publié sur GitHub (les fichiers Mixamo ne peuvent pas être redistribués tels quels).
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "personnages_fournis")
MODEL_EXTS = (".fbx", ".glb", ".gltf", ".blend")


def character_id(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return (re.sub(r"[^A-Za-z0-9_\-]+", "_", s).strip("_")[:40] or "personnage").lower()


def _pretty_name(stem):
    """« YBot » -> « Y Bot », « x_bot » -> « x bot »."""
    s = re.sub(r"[_\-]+", " ", stem)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])", " ", s).strip() or stem


def default_character(studio_dir, cfg):
    """Personnage animé après chaque traitement : celui choisi dans Studio, sinon le premier
    personnage fourni (Y Bot), sinon aucun (le squelette seul est exporté)."""
    chars = list_characters(studio_dir)
    cid = (cfg or {}).get("character") or ""
    if any(c["id"] == cid for c in chars):
        return cid
    return next((c["id"] for c in chars if c.get("builtin")), "")


def install_bundled_characters(studio_dir, blender=None, log=print, bundled_dir=None):
    """Installe les personnages du dossier personnages_fournis/ (une fois) et les marque
    « fournis ». Renvoie les identifiants installés ou mis à jour."""
    src_dir = bundled_dir or BUNDLED_DIR
    if not os.path.isdir(src_dir):
        return []
    root = characters_dir(studio_dir)
    os.makedirs(root, exist_ok=True)
    done = []
    for fn in sorted(os.listdir(src_dir)):
        stem, ext = os.path.splitext(fn)
        if ext.lower() not in MODEL_EXTS:
            continue
        cid = character_id(stem)
        d = os.path.join(root, cid)
        mf = os.path.join(d, "personnage.json")
        if os.path.exists(mf):                      # déjà là (importé à la main) : marqué fourni
            try:
                with open(mf, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            if not m.get("builtin"):
                m["builtin"] = True
                if m.get("name") == stem:
                    m["name"] = _pretty_name(stem)
                with open(mf, "w", encoding="utf-8") as f:
                    json.dump(m, f, indent=2, ensure_ascii=False)
                done.append(cid)
            continue
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, fn)
        shutil.copy2(os.path.join(src_dir, fn), dst)
        info = fbx.inspect_character(dst, os.path.join(d, "apercu.glb"), blender, log)
        if not info:
            shutil.rmtree(d, ignore_errors=True)
            log(f"personnage fourni {fn} non installé (Blender introuvable ou fichier illisible)")
            continue
        with open(mf, "w", encoding="utf-8") as f:
            json.dump(dict(name=_pretty_name(stem), file=fn, info=info, builtin=True,
                           created=time.strftime("%Y-%m-%d %H:%M:%S")), f, indent=2, ensure_ascii=False)
        log(f"personnage fourni installé : {_pretty_name(stem)}")
        done.append(cid)
    return done


def rig_preview_path(take_dir):
    """Aperçu 3D du rig Mixamo de la prise (avec le mannequin par défaut)."""
    name = os.path.basename(os.path.normpath(take_dir))
    return os.path.join(take_dir, "mocapcam", f"{name}_rig.glb")


def character_outputs(take_dir, cid):
    name = os.path.basename(os.path.normpath(take_dir))
    return (os.path.join(take_dir, f"{name}_{cid}.fbx"),
            os.path.join(take_dir, "mocapcam", f"{name}_{cid}.glb"))


def animate_character(take_dir, studio_dir, cid, log=print, blender=None):
    """Anime un personnage riggé avec la prise : <prise>_<personnage>.fbx dans la prise."""
    name = os.path.basename(os.path.normpath(take_dir))
    anim = os.path.join(take_dir, f"{name}.fbx")
    if not os.path.exists(anim):
        raise FileNotFoundError("FBX de la prise absent : traitez-la d'abord")
    ch = get_character(studio_dir, cid)
    if ch is None:
        raise FileNotFoundError(f"personnage inconnu : {cid}")
    src = os.path.join(characters_dir(studio_dir), cid, ch["file"])
    out, glb = character_outputs(take_dir, cid)
    os.makedirs(os.path.dirname(glb), exist_ok=True)
    if blender is None:
        blender = load_studio(studio_dir).get("blender") or None
    log(f"Animation de « {ch.get('name', cid)} » avec {name}…")
    rep = fbx.retarget(anim, src, out, glb, blender=blender, log=log)
    if not rep:
        raise RuntimeError("le personnage n'a pas pu être animé (voir le journal)")
    return out


def _animated_characters(take_dir, studio_dir):
    return [c["id"] for c in list_characters(studio_dir) if os.path.exists(character_outputs(take_dir, c["id"])[0])]


def _load_positions(data):
    return np.array([[[np.nan] * 3 if p is None else p for p in fr] for fr in data["positions"]], float)


def rescale_take(take_dir, height_cm, blender=None, log=print, studio_dir=None):
    """Met une prise déjà traitée à la taille indiquée (cm), sans refaire le traitement,
    puis recrée son FBX. Retourne le chemin du FBX (ou de la capture si pas de Blender)."""
    name = os.path.basename(os.path.normpath(take_dir))
    path = os.path.join(take_dir, "mocapcam", f"{name}.mocap.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prise non traitée : {take_dir}")
    height_cm = float(height_cm)
    if not 50 <= height_cm <= 260:
        raise ValueError("taille entre 50 et 260 cm")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    X = _load_positions(data)
    h = rig_height(X)
    if h is None:
        raise RuntimeError("squelette incomplet : taille impossible à mesurer")
    s = height_cm / 100.0 / h
    cams = [dict(c) for c in data.get("cameras", [])]
    vol = (data["volume"]["min"], data["volume"]["max"]) if "volume" in data else ([0, 0, 0], [0, 0, 0])
    X, cams, vol = scale_world(X, cams, vol, s)
    data["positions"] = [[None if not np.isfinite(p).all() else [round(float(v), 4) for v in p]
                          for p in frame] for frame in X]
    for c in cams:
        c["t"] = np.asarray(c["t"]).tolist()
    data["cameras"] = cams
    data["volume"] = {"min": np.asarray(vol[0]).tolist(), "max": np.asarray(vol[1]).tolist()}
    st = data.setdefault("stats", {})
    st["scale_applied"] = float(st.get("scale_applied") or 1.0) * s
    st["actor_height_m"] = rig_height(X)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), default=_jsonable)
    meta_path = os.path.join(take_dir, "take.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    meta["actor_height_cm"] = round(height_cm, 1)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    rp = os.path.join(take_dir, "mocapcam", "rapport.json")
    if os.path.exists(rp):                              # taille affichée par Studio
        try:
            with open(rp, encoding="utf-8") as f:
                rep = json.load(f)
            rep["actor_height_m"] = st["actor_height_m"]
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=2, ensure_ascii=False, default=_jsonable)
        except (OSError, ValueError):
            pass
    log(f"{name} : squelette mis à {height_cm:.0f} cm (× {s:.3f})")
    if os.path.exists(rig_preview_path(take_dir)):     # aperçu à l'ancienne taille : refait
        os.remove(rig_preview_path(take_dir))
    out = path
    if blender is not False:
        cfg = load_studio(studio_dir) if studio_dir else {}
        chars = _animated_characters(take_dir, studio_dir) if studio_dir else []
        out = fbx.export(path, os.path.join(take_dir, f"{name}.fbx"),
                         blender=blender or cfg.get("blender") or None, log=log,
                         glb_path=rig_preview_path(take_dir)) or path
        for cid in chars:          # personnages déjà animés avec cette prise : refaits à la bonne taille
            try:
                animate_character(take_dir, studio_dir, cid, log=log, blender=blender or cfg.get("blender") or None)
            except (RuntimeError, FileNotFoundError) as e:
                log(f"  {cid} : {e}")
    return out


def process_take(studio_dir, take_dir, force=False, manual_delays=None, only_calib=False,
                 log=print):
    t0 = time.time()
    cfg = load_studio(studio_dir)
    work = os.path.join(take_dir, "mocapcam")
    os.makedirs(work, exist_ok=True)
    meta = load_take_meta(take_dir)
    if meta:   # prise MocapCam Studio : les caméras sont celles de la prise
        names = [n for n, c in meta["cameras"].items() if c.get("file")]
    else:
        names = cfg["cameras"]
    raw = video.find_camera_videos(take_dir, names)
    fps = cfg["fps"]

    log("1/6 Normalisation des vidéos (cadence fixe)…")
    norm, cam_fps = {}, {}
    for n, p in raw.items():
        # chaque vidéo garde sa cadence d'origine (30 ou 60 i/s…) : pas d'images dupliquées
        cam_fps[n] = min(video.native_fps(p), 120)
        norm[n] = video.normalize(p, os.path.join(work, f"norm_{n}_{cam_fps[n]}.mp4"), cam_fps[n],
                                  cfg["max_height"], force=force)
    fps = max(cam_fps.values())          # base de temps commune = la plus rapide
    log("  cadences : " + ", ".join(f"{n} {f} i/s" for n, f in cam_fps.items())
        + (f" -> interpolées sur une base commune de {fps} i/s" if len(set(cam_fps.values())) > 1 else ""))

    log("2/6 Synchronisation par le son…")
    delays = compute_delays(norm, manual_delays, meta, log)
    delays_s = list(delays)

    log("3/6 Calibration automatique par les boules au sol…")
    cams, vol = calibrate_extrinsics(cfg, studio_dir, norm, work, log, meta=meta)
    if only_calib:
        log(f"Images de contrôle dans {work}")
        return None

    log("4/6 Détection 2D corps + mains (long la première fois)…")
    poses = []
    for n, p in norm.items():
        poses.append(pose2d.run_video(p, os.path.join(work, f"pose2d_{n}_{cam_fps[n]}.npz"),
                                      force=force, log=log, **cfg["pose"]))

    # toutes les caméras ramenées sur la même base de temps (décalage sous-image inclus)
    names = list(norm)
    T = int(poses[0]["kp"].shape[0] / cam_fps[names[0]] * fps)
    if not manual_delays and cfg.get("sync_by_motion", True):
        # le bip date le son ; l'image d'un téléphone peut être décalée de son propre son
        wide = not ((meta or {}).get("chirps_at_pc") or (meta or {}).get("chirp_at_pc"))
        delays = syncbody.refine(cams, poses, names, [cam_fps[n] for n in names], fps, delays, T, vol,
                                 cfg["reconstruction"], log, search_s=1.0 if wide else 0.35)
    delays_body = list(delays)
    poses = [triangulate.resample_pose(p, cam_fps[n], fps, d, T) for p, n, d in zip(poses, names, delays)]
    delays = [0.0] * len(poses)

    log("5/6 Affinage des caméras avec le corps de l'acteur, puis triangulation 3D…")
    cams, ac = autocalib.refine(cams, poses, delays, fps, vol, cfg["reconstruction"],
                                fixed_intrinsics=[c["intrinsics_fixed"] for c in cams], log=log)
    if ac:
        log(f"  {ac['observations']} observations ; erreur {ac['reproj_before_px']:.1f} px "
            f"-> {ac['reproj_after_px']:.1f} px")
        for c in ac["cameras"]:
            log(f"  {c['name']} : focale {c['focal_px']:.0f} px, déformation k1 {c['k1']:+.3f} k2 {c['k2']:+.3f}")
    X, stats, choices = triangulate.reconstruct(cams, poses, delays, fps, vol,
                                                cfg["reconstruction"], log, return_choices=True)
    log(f"  images reconstruites : {stats['frames_ok']}/{stats['frames']}, "
        f"erreur médiane {stats['median_reproj_px'] or float('nan'):.1f} px, "
        f"inversions gauche/droite corrigées : {stats['lr_swaps_fixed']}")

    log("6/6 Nettoyage, affinage global du mouvement (quelques minutes), export FBX…")
    X = triangulate.postprocess(X, fps, cfg["filtering"])
    T0 = X.shape[0]
    X, first = triangulate.trim_take(X, fps)
    stats["first_frame"] = int(first)
    if X.shape[0] < T0:
        log(f"  début/fin sans acteur coupés : {first / fps:.1f} s au début, "
            f"{(T0 - first - X.shape[0]) / fps:.1f} s à la fin")
    rcfg = dict(cfg["reconstruction"], **cfg.get("refine", {}))
    if rcfg.get("enabled", True):
        X, stats["global_refine"] = motion_opt.optimize(X, cams, poses, choices, fps, rcfg, log,
                                                        frames=np.arange(first, first + X.shape[0]))
    ok = np.isfinite(X[..., 0])
    stats["coverage_body"] = float(ok[:, :23].mean())
    stats["coverage_hands"] = float(ok[:, 91:].mean())
    stats["delays_s"] = dict(zip(norm.keys(), delays_body))
    stats["delays_sound_s"] = dict(zip(norm.keys(), delays_s))
    stats["camera_fps"] = cam_fps
    stats["autocalibration"] = ac
    h = rig_height(X)
    target = target_height_m(cfg, meta)
    stats["actor_height_raw_m"] = h
    if h is not None and target is not None:
        s = target / h
        X, cams, vol = scale_world(X, cams, vol, s)
        stats["scale_applied"] = s
        h = rig_height(X)
        log(f"  taille de l'acteur : {h * 100:.0f} cm (indiquée), mise à l'échelle × {s:.3f}")
    elif h is not None:
        log(f"  taille de l'acteur mesurée : {h * 100:.0f} cm (aucune taille indiquée)")
        if not 1.35 <= h <= 2.15:
            log("  ATTENTION : taille peu plausible. Indiquez la taille de l'acteur en cm dans "
                "MocapCam Studio : le squelette sera mis à la bonne taille.")
    stats["actor_height_m"] = h
    name = os.path.basename(os.path.normpath(take_dir))
    # points 3D bruts : données de travail (dans mocapcam/), d'où est tiré le FBX
    out = export_motion(os.path.join(work, f"{name}.mocap.json"), X, fps, cams, cfg, vol, stats)
    old = os.path.join(take_dir, f"{name}.mocap.json")      # ancien emplacement
    if os.path.exists(old):
        os.remove(old)
    with open(os.path.join(work, "rapport.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, default=_jsonable)
    result = out
    if cfg.get("fbx", True):
        result = fbx.export(out, os.path.join(take_dir, f"{name}.fbx"),
                            blender=cfg.get("blender") or None, log=log,
                            glb_path=rig_preview_path(take_dir)) or out
    cid = default_character(studio_dir, cfg)
    if cid and result.endswith(".fbx"):
        try:
            animate_character(take_dir, studio_dir, cid, log=log, blender=cfg.get("blender") or None)
        except (RuntimeError, FileNotFoundError) as e:
            log(f"  personnage non animé : {e}")
    log(f"Terminé en {time.time() - t0:.0f} s -> {result}")
    log(f"  couverture corps {stats['coverage_body'] * 100:.0f} %, mains {stats['coverage_hands'] * 100:.0f} %")
    return result
