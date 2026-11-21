"""Moteur Blender de MOCAM Studio (lancé sans interface : blender -b --python blender_script.py -- …).

Construit le squelette Mixamo animé à partir d'une capture, transfère l'animation sur un
personnage riggé (Mixamo, Unreal, Character Creator, Biped, DAZ, VRoid, Rigify), inspecte un
personnage et exporte le visage animé (52 blendshapes ARKit). Ce n'est pas un add-on.
"""

"""
Principe de l'application sur un squelette :
pour chaque os, on construit un repère à partir des points capturés (direction de l'os +
un vecteur de référence pour la torsion), et le même repère à partir de la pose de repos
du squelette cible. La rotation monde de l'os = repère_capture · repère_repos⁻¹ · rotation_repos.
Cela marche quelle que soit la pose de repos (T-pose, A-pose), le roll des os, l'échelle
et l'orientation de l'objet armature (FBX Mixamo à 0.01, tourné de 90°, etc.).
"""

import json
import math
import os

import bpy
import numpy as np
from mathutils import Matrix, Vector

PREFIX = "mixamorig:"

# --------------------------------------------------------------------------- hiérarchie Mixamo

FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def mixamo_hierarchy():
    """Liste ordonnée (os, parent) des 65 os Mixamo standard."""
    h = [("Hips", None), ("Spine", "Hips"), ("Spine1", "Spine"), ("Spine2", "Spine1"),
         ("Neck", "Spine2"), ("Head", "Neck"), ("HeadTop_End", "Head")]
    for s in ("Left", "Right"):
        h += [(f"{s}Shoulder", "Spine2"), (f"{s}Arm", f"{s}Shoulder"),
              (f"{s}ForeArm", f"{s}Arm"), (f"{s}Hand", f"{s}ForeArm")]
        for f in FINGERS:
            prev = f"{s}Hand"
            for k in range(1, 5):
                h.append((f"{s}Hand{f}{k}", prev))
                prev = f"{s}Hand{f}{k}"
    for s in ("Left", "Right"):
        h += [(f"{s}UpLeg", "Hips"), (f"{s}Leg", f"{s}UpLeg"), (f"{s}Foot", f"{s}Leg"),
              (f"{s}ToeBase", f"{s}Foot"), (f"{s}Toe_End", f"{s}ToeBase")]
    return h


# --------------------------------------------------------------------------- maths vectorisées

def nrm(v):
    v = np.asarray(v, float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return v / np.linalg.norm(v, axis=-1, keepdims=True)


def dot(a, b):
    return np.sum(a * b, axis=-1, keepdims=True)


def make_frames(d, r):
    """Repère orthonormé : Y = direction de l'os, Z = référence (orthogonalisée)."""
    y = nrm(d)
    z = r - dot(r, y) * y
    zn = np.linalg.norm(z, axis=-1)
    z = nrm(z)
    x = np.cross(y, z)
    F = np.stack([x, y, z], axis=-1)
    dn = np.linalg.norm(np.asarray(d, float), axis=-1)
    valid = np.isfinite(F).all(axis=(-1, -2)) & (zn > 1e-6) & (dn > 1e-6)
    return F, valid


def hold_fill(F, valid):
    """Images invalides : on garde la dernière image valide (la première au tout début).
    Pas de bascule au milieu d'un trou : la pose reste figée puis reprend quand les
    données reviennent."""
    T = F.shape[0]
    if valid.all():
        return F
    idx = np.where(valid)[0]
    if idx.size == 0:
        return None
    pos = np.searchsorted(idx, np.arange(T), side="right") - 1
    pick = idx[np.clip(pos, 0, idx.size - 1)]
    return F[pick]


def mat_to_quat(M):
    """(T,3,3) -> (T,4) quaternions (w, x, y, z), continus dans le temps."""
    M = np.asarray(M)
    T = M.shape[0]
    q = np.zeros((T, 4))
    tr = M[:, 0, 0] + M[:, 1, 1] + M[:, 2, 2]
    for i in range(T):
        m = M[i]
        if tr[i] > 0:
            s = math.sqrt(tr[i] + 1.0) * 2
            q[i] = [0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
            q[i] = [(m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s]
        elif m[1, 1] > m[2, 2]:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
            q[i] = [(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s]
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
            q[i] = [(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s]
    q = nrm(q)
    for i in range(1, T):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    return q


def np_mat(m):
    return np.array([list(r) for r in m], float)


# --------------------------------------------------------------------------- points capturés

def load_motion(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    names = d["joint_names"]
    T = d["frame_count"]
    X = np.full((T, len(names), 3), np.nan)
    for i, fr in enumerate(d["positions"]):
        for j, p in enumerate(fr):
            if p is not None:
                X[i, j] = p
    J = {n: X[:, i] for i, n in enumerate(names)}
    return d, J


def derive(J):
    """Ajoute les points/axes dérivés utilisés par les repères des os."""
    J["mid_hip"] = (J["l_hip"] + J["r_hip"]) / 2
    J["mid_sh"] = (J["l_shoulder"] + J["r_shoulder"]) / 2
    J["mid_ear"] = (J["l_ear"] + J["r_ear"]) / 2
    J["lat_hip"] = J["l_hip"] - J["r_hip"]
    J["lat_sh"] = J["l_shoulder"] - J["r_shoulder"]
    J["lat_ear"] = J["l_ear"] - J["r_ear"]
    J["up_t"] = J["mid_sh"] - J["mid_hip"]
    J["fwd_t"] = np.cross(nrm(J["lat_hip"]) + nrm(J["lat_sh"]), J["up_t"])
    for s in ("l", "r"):
        J[f"{s}_toes"] = (J[f"{s}_big_toe"] + J[f"{s}_small_toe"]) / 2
    J["l_foot_lat"] = J["l_small_toe"] - J["l_big_toe"]
    J["r_foot_lat"] = J["r_big_toe"] - J["r_small_toe"]
    return J


def bone_specs():
    """os Mixamo -> (fonction direction, fonction référence de torsion)."""
    S = {
        "Hips": (lambda J: J["up_t"], lambda J: J["lat_hip"]),
        "Spine": (lambda J: J["up_t"], lambda J: 0.67 * nrm(J["lat_hip"]) + 0.33 * nrm(J["lat_sh"])),
        "Spine1": (lambda J: J["up_t"], lambda J: 0.33 * nrm(J["lat_hip"]) + 0.67 * nrm(J["lat_sh"])),
        "Spine2": (lambda J: J["up_t"], lambda J: J["lat_sh"]),
        "Neck": (lambda J: J["mid_ear"] - J["mid_sh"], lambda J: nrm(J["lat_sh"]) + nrm(J["lat_ear"])),
        "Head": (lambda J: np.cross(J["nose"] - J["mid_ear"], J["lat_ear"]), lambda J: J["lat_ear"]),
    }
    for s, side, h in (("l", "Left", "lh"), ("r", "Right", "rh")):
        S[f"{side}Shoulder"] = (lambda J, s=s: J[f"{s}_shoulder"] - J["mid_sh"],
                                lambda J: J["up_t"])
        S[f"{side}Arm"] = (
            lambda J, s=s: J[f"{s}_elbow"] - J[f"{s}_shoulder"],
            lambda J, s=s: np.cross(J[f"{s}_elbow"] - J[f"{s}_shoulder"],
                                    nrm(J[f"{s}_wrist"] - J[f"{s}_elbow"]) + 0.3 * nrm(J["fwd_t"])))
        S[f"{side}ForeArm"] = (lambda J, s=s: J[f"{s}_wrist"] - J[f"{s}_elbow"],
                               lambda J, h=h: J[f"{h}_5"] - J[f"{h}_17"])
        S[f"{side}Hand"] = (lambda J, h=h: J[f"{h}_9"] - J[f"{h}_0"],
                            lambda J, h=h: J[f"{h}_5"] - J[f"{h}_17"])
        for f, base in (("Index", 5), ("Middle", 9), ("Ring", 13), ("Pinky", 17)):
            for k in range(3):
                S[f"{side}Hand{f}{k + 1}"] = (
                    lambda J, h=h, a=base + k: J[f"{h}_{a + 1}"] - J[f"{h}_{a}"],
                    lambda J, h=h: J[f"{h}_5"] - J[f"{h}_17"])
        for k in range(3):
            S[f"{side}HandThumb{k + 1}"] = (
                lambda J, h=h, a=1 + k: J[f"{h}_{a + 1}"] - J[f"{h}_{a}"],
                lambda J, h=h: np.cross(J[f"{h}_9"] - J[f"{h}_0"], J[f"{h}_5"] - J[f"{h}_17"]))
        # le genou regarde du côté où pointe le pied : la torsion de la cuisse suit le pied
        # quand la jambe est tendue (le plan hanche-genou-cheville n'est alors pas défini)
        S[f"{side}UpLeg"] = (
            lambda J, s=s: J[f"{s}_knee"] - J[f"{s}_hip"],
            lambda J, s=s: np.cross(J[f"{s}_knee"] - J[f"{s}_hip"],
                                    nrm(J[f"{s}_ankle"] - J[f"{s}_knee"])
                                    - nrm(nrm(J[f"{s}_toes"] - J[f"{s}_heel"]) + 0.3 * nrm(J["fwd_t"]))))
        S[f"{side}Leg"] = (lambda J, s=s: J[f"{s}_ankle"] - J[f"{s}_knee"],
                           lambda J, s=s: nrm(J[f"{s}_toes"] - J[f"{s}_heel"]) + 0.3 * nrm(J["fwd_t"]))
        S[f"{side}Foot"] = (lambda J, s=s: J[f"{s}_toes"] - J[f"{s}_ankle"],
                            lambda J, s=s: J[f"{s}_foot_lat"] + 0.5 * nrm(J["lat_hip"]))
    return S


# --------------------------------------------------------------------------- armature cible

def find_prefix(obj):
    for b in obj.data.bones:
        if b.name.endswith("Hips"):
            return b.name[: -len("Hips")]
    raise RuntimeError("Aucun os 'Hips' : ce n'est pas un squelette au format Mixamo.")


# --- correspondance des noms d'os : Mixamo, Unreal, Character Creator, Biped (3ds Max), DAZ,
#     VRoid / Unity, Rigify (os DEF-)… -> noms courts Mixamo (Hips, LeftArm, LeftHandIndex1…)

import re as _re

_IGNORE_TOKENS = {"mixamorig", "def", "cc", "base", "bip", "j", "b", "bone", "jnt", "joint", "sk", "rig",
                  "armature", "character", "skeleton", "c"}
_EXCLUDE = ("twist", "ik", "pole", "target", "ctrl", "control", "mch", "org", "roll", "share", "metacarpal",
            "carpal", "corrective", "helper", "prop", "weapon", "eye", "jaw", "tongue", "teeth", "brow",
            "lid", "breast", "pectoral", "upperarmtwist", "nub", "attach", "socket", "camera", "tail")
_FINGER_KEYS = (("thumb", "Thumb"), ("index", "Index"), ("middle", "Middle"), ("mid", "Middle"),
                ("ring", "Ring"), ("pinky", "Pinky"), ("little", "Pinky"), ("small", "Pinky"))


def _tokens(name):
    n = _re.split(r"[:|]", name)[-1]
    return [t.lower() for t in _re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", n)]


def classify_bone(name):
    """(côté 'Left'/'Right'/None, partie, numéro) d'après le nom d'un os, ou None."""
    toks = _tokens(name)
    side = None
    rest, digits = [], []
    for i, t in enumerate(toks):
        if t in ("l", "left") and side is None:
            side = "Left"
        elif t in ("r", "right") and side is None:
            side = "Right"
        elif t.isdigit():
            digits.append(t)
        elif t not in _IGNORE_TOKENS:
            rest.append(t)
    word = "".join(rest)
    if word.startswith("neck") and side is None:      # Character Creator : « NeckTwist01 » = cou
        return (None, "Neck", int(digits[-1]) if digits else 0)
    if not word or any(x in word for x in _EXCLUDE):
        return None
    end = word.endswith("end") or word.endswith("top") or word.endswith("tip")
    # doigts (Biped : « Finger0 » = pouce, « Finger01 » = pouce, 2e phalange…)
    if "finger" in word and digits and not any(k in word for k, _ in _FINGER_KEYS):
        d = digits[-1]
        f = int(d[0])
        k = 1 if len(d) == 1 else int(d[1:]) + 1
        return (side, ("Thumb", "Index", "Middle", "Ring", "Pinky")[min(f, 4)], k) if side else None
    for key, f in _FINGER_KEYS:
        if key in word and (word.startswith(key) or "hand" in word or "f" + key in word or len(word) <= len(key) + 4):
            k = int(digits[-1]) if digits else 1
            if end:
                k = 4
            return (side, f, k) if side else None
    if side:
        if word in ("hand", "wrist"):
            return (side, "Hand", 0)
        if word in ("forearm", "lowerarm", "elbow", "forearmbend", "loarm", "lowarm"):
            return (side, "ForeArm", 0)
        if word in ("arm", "upperarm", "uparm", "shldrbend", "shldr", "upperarmbend"):
            return (side, "Arm", 0)
        if word in ("shoulder", "clavicle", "collar", "clav"):
            return (side, "Shoulder", 0)
        if word in ("upleg", "thigh", "upperleg", "thighbend", "upleg", "hip"):
            return (side, "UpLeg", 0)
        if word in ("leg", "calf", "shin", "lowerleg", "knee", "loleg", "lowleg"):
            return (side, "Leg", 0)
        if word in ("foot", "ankle"):
            return (side, "Foot", 0)
        if word in ("toeend", "toesend", "toetip", "toeend"):
            return (side, "Toe_End", 0)
        if word in ("toe", "toes", "toebase", "ball", "toesbase"):
            return (side, "ToeBase", 0)
        return None
    if word in ("hips", "hip", "pelvis", "root", "cog"):
        return (None, "Hips", 0)
    if word.startswith("spine") or word in ("chest", "upperchest", "waist", "abdomen", "abdomenlower",
                                            "abdomenupper", "chestlower", "chestupper", "torso", "back"):
        return (None, "Spine", int(digits[-1]) if digits else 0)
    if word.startswith("neck"):
        return (None, "Neck", int(digits[-1]) if digits else 0)
    if word == "head":
        return (None, "Head", 0)
    if word in ("headtop", "headend", "headtopend", "headnub"):
        return (None, "HeadTop_End", 0)
    return None


def _depth(b):
    d = 0
    while b.parent is not None:
        b, d = b.parent, d + 1
    return d


def _chain(b, top):
    """Os de b (exclu) jusqu'à top (exclu), en remontant."""
    out = []
    p = b.parent
    while p is not None and p != top:
        out.append(p)
        p = p.parent
    return out[::-1] if p == top else None


def bone_map(obj):
    """{nom court Mixamo : nom de l'os} pour une armature (noms Mixamo ou d'un autre rig)."""
    bones = obj.data.bones
    try:                               # squelette Mixamo : simple préfixe
        pre = find_prefix(obj)
        m = {b.name[len(pre):]: b.name for b in bones if b.name.startswith(pre)}
        if all(k in m for k in ("Hips", "LeftArm", "LeftUpLeg", "RightUpLeg", "Head")):
            return m
    except RuntimeError:
        pass
    # Rigify (os DEF-spine, DEF-spine.001…) : la colonne n'a pas de nom de partie
    names = {b.name for b in bones}
    if "DEF-spine" in names and "DEF-spine.006" in names:
        pre = {"Hips": "DEF-spine", "Spine": "DEF-spine.001", "Spine1": "DEF-spine.002",
               "Spine2": "DEF-spine.003", "Neck": "DEF-spine.004", "Head": "DEF-spine.006"}
    else:
        pre = {}
    cands = {}
    rigify = "DEF-spine" in names
    for b in bones:
        if rigify and not b.name.startswith("DEF-"):
            continue      # Rigify : seuls les os DEF- déforment le maillage (le reste = contrôleurs)
        c = classify_bone(b.name)
        if c is None:
            continue
        side, part, k = c
        cands.setdefault((side, part), []).append((k, b))
    m = dict(pre)

    def pick(lst):          # os déformant, le plus haut dans la hiérarchie
        return sorted(lst, key=lambda kb: (not kb[1].use_deform, _depth(kb[1]), kb[0]))[0][1]

    for side in ("Left", "Right"):
        for part in ("Shoulder", "Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot", "ToeBase", "Toe_End"):
            lst = cands.get((side, part))
            if lst:
                m.setdefault(f"{side}{part}", pick(lst).name)
        for f in FINGERS:
            lst = cands.get((side, f), [])
            # phalanges dans l'ordre de la hiérarchie (numérotation 0/1 selon les rigs)
            lst = sorted(lst, key=lambda kb: (_depth(kb[1]), kb[0]))
            seen = []
            for k, b in lst:
                if b.name not in seen:
                    seen.append(b.name)
            for i, nm in enumerate(seen[:4]):
                m.setdefault(f"{side}Hand{f}{i + 1}", nm)
    if "Head" not in m and cands.get((None, "Head")):
        m["Head"] = pick(cands[(None, "Head")]).name
    if "HeadTop_End" not in m and cands.get((None, "HeadTop_End")):
        m["HeadTop_End"] = pick(cands[(None, "HeadTop_End")]).name
    need = ("LeftUpLeg", "RightUpLeg", "LeftArm", "RightArm", "Head")
    if any(k not in m for k in need):
        missing = [k for k in need if k not in m]
        raise RuntimeError("Squelette non reconnu (os introuvables : " + ", ".join(missing) + "). "
                           "Noms d'os pris en charge : Mixamo, Unreal, Character Creator, Biped, DAZ, VRoid, Rigify.")
    B = {b.name: b for b in bones}
    lu, ru, head = B[m["LeftUpLeg"]], B[m["RightUpLeg"]], B[m["Head"]]
    if "Hips" not in m:
        # bassin : l'ancêtre commun le plus bas des deux cuisses et de la tête
        anc = []
        p = lu.parent
        while p is not None:
            anc.append(p)
            p = p.parent
        common = [a for a in anc if a in _ancestors(ru) and a in _ancestors(head)]
        if not common:
            raise RuntimeError("Squelette non reconnu : pas de bassin commun aux deux jambes.")
        m["Hips"] = common[0].name
    hips = B[m["Hips"]]
    if "Neck" not in m:
        necks = sorted(cands.get((None, "Neck"), []), key=lambda kb: _depth(kb[1]))
        if necks:
            m["Neck"] = necks[0][1].name
    if "Spine" not in m:
        top = B[m["Neck"]] if "Neck" in m else head
        ch = _chain(top, hips) or []
        ch = [b for b in ch if not any(b.name == v for v in m.values()) and "neck" not in b.name.lower()]
        if ch:
            if len(ch) >= 3:
                pickc = [ch[0], ch[len(ch) // 2], ch[-1]]
            elif len(ch) == 2:
                pickc = [ch[0], None, ch[1]]
            else:
                pickc = [None, None, ch[0]]
            for k, b in zip(("Spine", "Spine1", "Spine2"), pickc):
                if b is not None:
                    m[k] = b.name
    return m


def _ancestors(b):
    out = []
    p = b.parent
    while p is not None:
        out.append(p)
        p = p.parent
    return out


def _resolve_map(obj, bmap):
    """bmap : dict nom court -> os, ou préfixe (str, ancien appel), ou None (détection)."""
    if bmap is None:
        return bone_map(obj)
    if isinstance(bmap, str):
        return {b.name[len(bmap):]: b.name for b in obj.data.bones if b.name.startswith(bmap)}
    return bmap


def _bone_frames(obj, bmap, posed):
    bmap = _resolve_map(obj, bmap)
    mw = obj.matrix_world
    out = {}
    src = obj.pose.bones if posed else obj.data.bones
    for short, name in bmap.items():
        b = src.get(name)
        if b is None:
            continue
        m = mw @ (b.matrix if posed else b.matrix_local)
        tail = mw @ ((b.tail if posed else b.tail_local))
        out[short] = (np.array(m.translation), np_mat(m.to_3x3().normalized()), np.array(tail))
    return out


def virtual_joints(obj, bmap=None, posed=False):
    """Points équivalents aux 133 points capturés, calculés depuis les os
    (pose de repos, ou pose courante si posed=True)."""
    rest = _bone_frames(obj, bmap, False)
    cur = _bone_frames(obj, bmap, True) if posed else rest
    H = {k: v[0] for k, v in rest.items()}
    lat = nrm(H["LeftUpLeg"] - H["RightUpLeg"])
    up0 = H.get("Neck", H["Head"]) - H["Hips"]
    up = nrm(up0 - np.dot(up0, lat) * lat)
    fwd = np.cross(lat, up)
    leg = (np.linalg.norm(H["LeftUpLeg"] - H["LeftLeg"]) + np.linalg.norm(H["LeftLeg"] - H["LeftFoot"]))
    sc = leg / 0.85
    J = {}

    def attach(name, bone, off=np.zeros(3)):
        h_r, R_r, _ = rest[bone]
        h, R, _ = cur[bone]
        J[name] = h + R @ (R_r.T @ off)

    def tail_of(name, bone):
        h_r, R_r, t_r = rest[bone]
        attach(name, bone, t_r - h_r)

    def tip_of(name, bone, prev, ratio=0.8):
        """Bout d'un os terminal (dernière phalange…). La queue de l'os n'est fiable que si elle
        prolonge la chaîne : un FBX (Unreal, Maya…) importé dans Blender a souvent des queues
        orientées au hasard. Sinon, on prolonge l'os précédent."""
        h_r, R_r, t_r = rest[bone]
        d = h_r - rest[prev][0]
        L = np.linalg.norm(d)
        tl = t_r - h_r
        if L < 1e-9:
            attach(name, bone, tl)
        elif np.linalg.norm(tl) > 1e-9 and np.dot(nrm(tl), d / L) > 0.95:
            attach(name, bone, tl)
        else:
            attach(name, bone, d * ratio)

    attach("nose", "Head", sc * (0.09 * up + 0.10 * fwd))
    for s, sg in (("l", 1), ("r", -1)):
        attach(f"{s}_ear", "Head", sc * (0.07 * up + sg * 0.075 * lat))
        attach(f"{s}_eye", "Head", sc * (0.10 * up + 0.08 * fwd + sg * 0.032 * lat))
    for s, side, h, sg in (("l", "Left", "lh", 1), ("r", "Right", "rh", -1)):
        attach(f"{s}_shoulder", f"{side}Arm")
        if f"{side}ForeArm" in rest:
            attach(f"{s}_elbow", f"{side}ForeArm")
        else:
            tail_of(f"{s}_elbow", f"{side}Arm")
        hand = f"{side}Hand" if f"{side}Hand" in rest else None
        if hand:
            attach(f"{s}_wrist", hand)
        else:
            tail_of(f"{s}_wrist", f"{side}ForeArm" if f"{side}ForeArm" in rest else f"{side}Arm")
        attach(f"{s}_hip", f"{side}UpLeg")
        if f"{side}Leg" in rest:
            attach(f"{s}_knee", f"{side}Leg")
        else:
            tail_of(f"{s}_knee", f"{side}UpLeg")
        if f"{side}Foot" in rest:
            attach(f"{s}_ankle", f"{side}Foot")
        else:
            tail_of(f"{s}_ankle", f"{side}Leg")
        out = sg * lat
        foot = f"{side}Foot" if f"{side}Foot" in rest else (f"{side}Leg" if f"{side}Leg" in rest else f"{side}UpLeg")
        if f"{side}Toe_End" in rest:
            tip_bone, tip_off = f"{side}Toe_End", np.zeros(3)
        elif f"{side}ToeBase" in rest:
            tip_bone = f"{side}ToeBase"
            tip_off = rest[tip_bone][2] - rest[tip_bone][0]
            # orteils : vers l'avant, à plat. Queue d'os incohérente (FBX importé) : on prolonge
            # la direction cheville -> base des orteils, à l'horizontale
            fw = rest[tip_bone][0] - rest[foot][0]
            fw = fw - np.dot(fw, up) * up
            if np.linalg.norm(fw) < 1e-9:
                fw = fwd * sc * 0.14
            if np.linalg.norm(tip_off) < 1e-9 or np.dot(nrm(tip_off), nrm(fw)) < 0.7:
                tip_off = nrm(fw) * max(0.4 * np.linalg.norm(fw), sc * 0.05)
        else:
            tip_bone, tip_off = foot, (rest[foot][2] - rest[foot][0]) + sc * 0.15 * fwd
            if np.dot(nrm(rest[foot][2] - rest[foot][0]), fwd) < 0.3:
                tip_off = sc * (0.15 * fwd - 0.06 * up)
        attach(f"{s}_big_toe", tip_bone, tip_off - sc * 0.02 * out)
        attach(f"{s}_small_toe", tip_bone, tip_off + sc * (0.03 * out - 0.03 * fwd))
        attach(f"{s}_heel", foot, sc * (-0.065 * up - 0.05 * fwd))
        hb = hand or (f"{side}ForeArm" if f"{side}ForeArm" in rest else f"{side}Arm")
        attach(f"{h}_0", hb, (rest[hb][2] - rest[hb][0]) if not hand else np.zeros(3))
        hdir = nrm(rest[hb][2] - rest[hb][0])
        palm = sc * 0.09
        side_v = nrm(np.cross(hdir, up)) if abs(np.dot(hdir, up)) < 0.95 else fwd
        base0 = (rest[hb][2] - rest[hb][0]) if not hand else np.zeros(3)
        for f, base, lat_off in (("Thumb", 1, 0.03), ("Index", 5, 0.022), ("Middle", 9, 0.0),
                                 ("Ring", 13, -0.02), ("Pinky", 17, -0.04)):
            for k in range(4):
                bn = f"{side}Hand{f}{k + 1}"
                if bn in rest:
                    attach(f"{h}_{base + k}", bn)
                elif k > 0 and f"{side}Hand{f}{k}" in rest:
                    pv = f"{side}Hand{f}{k - 1}" if k > 1 and f"{side}Hand{f}{k - 1}" in rest else hb
                    tip_of(f"{h}_{base + k}", f"{side}Hand{f}{k}", pv)
                else:
                    # pas d'os de doigt (main « moufle ») : points posés dans le prolongement de la main
                    attach(f"{h}_{base + k}", hb,
                           base0 + hdir * palm * (1.0 + 0.3 * k) + side_v * sc * lat_off * sg)
    return J


def _ordered_bones(arm):
    order = []

    def rec(b):
        order.append(b)
        for c in b.children:
            rec(c)
    for b in arm.bones:
        if b.parent is None:
            rec(b)
    return order


def _scale_from(J):
    leg = []
    for s in ("l", "r"):
        a = np.linalg.norm(J[f"{s}_hip"] - J[f"{s}_knee"], axis=-1)
        b = np.linalg.norm(J[f"{s}_knee"] - J[f"{s}_ankle"], axis=-1)
        leg.append(a + b)
    v = np.concatenate([np.atleast_1d(x) for x in leg])
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size else None


def apply_motion(obj, J_mocap, fps, start_frame=1, root_mode="RELATIVE", action_name=None, clean=True,
                 twist=None):
    """Calcule et écrit l'animation sur l'armature obj (noms Mixamo, préfixe quelconque).
    clean=False : pas de lissage (source déjà propre, ex. une autre armature).
    twist : {os: (T,3)} références de torsion imposées (retarget : la torsion de chaque os
    de la source est reprise telle quelle au lieu d'être recalculée depuis les points)."""
    bmap = bone_map(obj)
    rev = {v: k for k, v in bmap.items()}
    Jm = derive(dict(J_mocap))
    Jr = derive(virtual_joints(obj, bmap, posed=False))
    specs = bone_specs()
    T = next(iter(J_mocap.values())).shape[0]
    mw = np_mat(obj.matrix_world)
    Rw = np_mat(obj.matrix_world.to_3x3().normalized())
    arm = obj.data
    rest_rot = {b.name: np_mat(b.matrix_local.to_3x3().normalized()) for b in arm.bones}
    M = {}
    P = {}            # position de la tête de chaque os (espace armature), image par image
    head_l = {b.name: np.array(b.head_local) for b in arm.bones}
    lpar = dict(mixamo_hierarchy())
    legu = (np.linalg.norm(head_l[bmap["LeftUpLeg"]] - head_l[bmap["LeftFoot"]])
            if "LeftFoot" in bmap else 1.0)
    channels = []
    report = {"animated": 0, "static": 0}

    def logical_parent(short):
        """Os piloté le plus proche au-dessus dans la hiérarchie Mixamo (cuisse -> bassin…)."""
        k = lpar.get(short)
        while k is not None and k not in bmap:
            k = lpar.get(k)
        return bmap.get(k) if k else None

    for b in _ordered_bones(arm):
        short = rev.get(b.name)
        Rb = rest_rot[b.name]
        p = b.parent
        Mb = None
        delta = None
        if short in specs:
            dfn, rfn = specs[short]
            with np.errstate(invalid="ignore"):
                dm = dfn(Jm)
                rm = twist[short] if (twist is not None and short in twist) else rfn(Jm)
                # référence de torsion absente (ex. main cachée) : on garde la dernière
                # torsion connue, mais la direction de l'os continue de suivre la capture
                rv = np.isfinite(rm).all(-1) & (np.linalg.norm(np.nan_to_num(rm), axis=-1) > 1e-9)
                if rv.any() and not rv.all():
                    rm = hold_fill(rm, rv)
                if clean and short in HAND_DRIVEN:
                    # la torsion vient des points de la main (petits, bruités) : lissée plus fort
                    # que la direction de l'os, qui vient du coude et du poignet (fiables)
                    if short.endswith("Hand"):
                        dm = smooth_vectors(dm, fps, cutoff=5.0, keep_norm=True)
                    rm = stable_twist(dm, rm, fps)
                elif clean and short in LEG_TWIST:
                    # torsion des jambes et des pieds : lissée autour de l'os (petites rotations)
                    rm = stable_twist(dm, rm, fps, cutoff=4.0, spike_deg=25.0, win=11)
                Fm, valid = make_frames(dm, rm)
                Fr, vr = make_frames(dfn(Jr), rfn(Jr))
            Fm = hold_fill(Fm, valid) if bool(vr) else None
            if Fm is not None:
                delta = Fm @ Fr.T                       # rotation monde capture vs repos
                W = delta @ (Rw @ Rb)
                Mb = np.einsum("ij,tjk->tik", Rw.T, W)  # espace armature
        if Mb is None:
            report["static"] += 1
            if p is not None:
                Mb = M[p.name] @ (rest_rot[p.name].T @ Rb)
            else:
                Mb = np.repeat(Rb[None], T, 0)
        else:
            report["animated"] += 1
        M[b.name] = Mb
        if p is not None:
            B = np.einsum("ij,tjk->tik", Rb.T @ rest_rot[p.name], np.transpose(M[p.name], (0, 2, 1)) @ Mb)
            fk = P[p.name] + np.einsum("tij,j->ti", M[p.name], rest_rot[p.name].T @ (head_l[b.name] - head_l[p.name]))
        else:
            B = np.einsum("ij,tjk->tik", Rb.T, Mb)
            fk = np.repeat(head_l[b.name][None], T, 0)
        P[b.name] = fk
        # os dont le parent réel n'est pas le parent logique (Rigify : cuisses sous ORG-spine,
        # bras sous ORG-shoulder, qui ne bougent pas) : on le rattache par une translation
        loc_keys = None
        lp = logical_parent(short) if (delta is not None and short not in (None, "Hips")) else None
        if lp is not None and lp in P and (p is None or lp != p.name):
            target = P[lp] + np.einsum("tij,j->ti", M[lp], rest_rot[lp].T @ (head_l[b.name] - head_l[lp]))
            dev = target - fk
            if np.nanmax(np.linalg.norm(dev, axis=1)) > 1e-3 * legu:
                Rl = (np.einsum("tij,jk->tik", M[p.name], rest_rot[p.name].T @ Rb) if p is not None
                      else np.repeat(Rb[None], T, 0))
                loc_keys = np.einsum("tji,tj->ti", Rl, dev)
                P[b.name] = target
        if delta is None and p is not None:
            continue  # os non piloté : on laisse sa pose de repos
        if loc_keys is not None:
            dp = f'pose.bones["{b.name}"].location'
            for k in range(3):
                channels.append((dp, k, loc_keys[:, k], b.name))
        q = mat_to_quat(B)
        q = clean_rotations(q, fps) if clean else continuous_quats(q)
        dp = f'pose.bones["{b.name}"].rotation_quaternion'
        for k in range(4):
            channels.append((dp, k, q[:, k], b.name))
        obj.pose.bones[b.name].rotation_mode = "QUATERNION"

        if short == "Hips" and delta is not None:
            # déplacement racine, mis à l'échelle du personnage
            s_m, s_t = _scale_from(Jm), _scale_from(Jr)
            s = (s_t / s_m) if (s_m and s_t) else 1.0
            mh = Jm["mid_hip"].copy()
            okm = np.isfinite(mh).all(1)
            if okm.any():
                idx = np.where(okm)[0]
                mh = np.stack([np.interp(np.arange(T), idx, mh[idx, k]) for k in range(3)], 1)
                h0 = np.array((obj.matrix_world @ b.matrix_local).translation)
                off = h0 - Jr["mid_hip"]
                if root_mode == "RELATIVE":
                    pos = np.empty_like(mh)
                    pos[:, :2] = Jr["mid_hip"][:2] + s * (mh[:, :2] - mh[idx[0], :2])
                    pos[:, 2] = mw[2, 3] + s * mh[:, 2]
                else:
                    pos = s * mh
                pos = pos + np.einsum("tij,j->ti", delta, off)
                inv = np.linalg.inv(mw)
                pa = (np.c_[pos, np.ones(T)] @ inv.T)[:, :3]   # espace armature
                P[b.name] = pa.copy()
                rest4 = np_mat(b.matrix_local)
                if p is not None:
                    rest4 = np.linalg.inv(np_mat(p.matrix_local)) @ rest4
                    pa = (np.c_[pa, np.ones(T)] @ np.linalg.inv(np_mat(p.matrix_local)).T)[:, :3]
                Rinv = np.linalg.inv(rest4[:3, :3])
                loc = (pa - rest4[:3, 3]) @ Rinv.T
                dp = f'pose.bones["{b.name}"].location'
                for k in range(3):
                    channels.append((dp, k, loc[:, k], b.name))
            report["scale"] = s

    _attach_followers(obj, bmap, specs, M, P, rest_rot, head_l, T, channels, report)
    name = action_name or f"MocapCam_{obj.name}"
    write_action(obj, name, start_frame, channels)
    return report


def _weighted_bones(obj):
    """Os qui déforment réellement un maillage (groupe de sommets non vide)."""
    out = set()
    for m in bpy.data.objects:
        if m.type != "MESH" or not any(md.type == "ARMATURE" and md.object == obj for md in m.modifiers):
            continue
        names = {g.index: g.name for g in m.vertex_groups}
        for v in m.data.vertices:
            for g in v.groups:
                if g.weight > 1e-4 and g.group in names:
                    out.add(names[g.group])
    return out


def _attach_followers(obj, bmap, specs, M, P, rest_rot, head_l, T, channels, report):
    """Os déformants hors de la chaîne animée (Rigify : DEF-pelvis, DEF-breast sous des os ORG
    immobiles) : sinon ils restent en pose de repos et étirent le maillage. Chacun suit, de façon
    rigide, l'os piloté le plus proche (bassin, buste…)."""
    arm = obj.data
    driven = {bmap[k] for k in bmap if k in specs and bmap[k] in M}
    weighted = _weighted_bones(obj)

    def has_driven_ancestor(b):
        p = b.parent
        while p is not None:
            if p.name in driven:
                return True
            p = p.parent
        return False

    segs = []
    for n in driven:
        b = arm.bones[n]
        kids = [c for c in b.children if c.name in driven]
        t = head_l[kids[0].name] if kids else np.array(b.tail_local)
        segs.append((n, head_l[n], t))
    done = set()
    count = 0
    for b in _ordered_bones(arm):
        if b.name in driven or b.name not in weighted or has_driven_ancestor(b):
            continue
        if any(a.name in done for a in _ancestors(b)):
            continue            # suit déjà un os rattaché plus haut
        h = head_l[b.name]

        def dist(seg):
            _, a, c = seg
            ab = c - a
            u = np.clip(np.dot(h - a, ab) / max(np.dot(ab, ab), 1e-12), 0, 1)
            return np.linalg.norm(a + u * ab - h)
        drv = min(segs, key=dist)[0]
        Rb = rest_rot[b.name]
        Mb = np.einsum("tij,jk->tik", M[drv], rest_rot[drv].T @ Rb)
        pos = P[drv] + np.einsum("tij,j->ti", M[drv], rest_rot[drv].T @ (h - head_l[drv]))
        p = b.parent
        if p is not None:          # parent immobile (pose de repos) : repère de repos du parent
            Rp = rest_rot[p.name]
            Bq = np.einsum("ij,tjk->tik", Rb.T @ Rp, np.einsum("ij,tjk->tik", Rp.T, Mb))
            loc = np.einsum("ij,tj->ti", Rb.T, pos - head_l[b.name])
        else:
            Bq = np.einsum("ij,tjk->tik", Rb.T, Mb)
            loc = np.einsum("ij,tj->ti", Rb.T, pos - head_l[b.name])
        q = continuous_quats(mat_to_quat(Bq))
        obj.pose.bones[b.name].rotation_mode = "QUATERNION"
        for k in range(4):
            channels.append((f'pose.bones["{b.name}"].rotation_quaternion', k, q[:, k], b.name))
        for k in range(3):
            channels.append((f'pose.bones["{b.name}"].location', k, loc[:, k], b.name))
        done.add(b.name)
        count += 1
    if count:
        report["followers"] = count


def lowpass(x, fps, cutoff):
    """Butterworth d'ordre 2, aller-retour (sans décalage), en numpy pur (Blender n'a pas
    scipy). x : (T, C)."""
    x = np.asarray(x, float)
    T = len(x)
    if cutoff >= fps / 2 or T < 8:
        return x
    K = np.tan(np.pi * cutoff / fps)
    nrm_ = 1.0 / (1.0 + np.sqrt(2.0) * K + K * K)
    b0 = K * K * nrm_
    b1, b2 = 2 * b0, b0
    a1 = 2 * (K * K - 1) * nrm_
    a2 = (1 - np.sqrt(2.0) * K + K * K) * nrm_
    n = int(min(T - 1, max(3, 3 * fps / cutoff)))
    xp = np.concatenate([2 * x[0] - x[n:0:-1], x, 2 * x[-1] - x[-2:-n - 2:-1]])

    def run(sig):
        y = np.empty_like(sig)
        y[0], y[1] = sig[0], sig[1]
        for i in range(2, len(sig)):
            y[i] = b0 * sig[i] + b1 * sig[i - 1] + b2 * sig[i - 2] - a1 * y[i - 1] - a2 * y[i - 2]
        return y

    y = run(run(xp)[::-1])[::-1]
    return y[n:n + T]


HAND_DRIVEN = {f"{s}{b}" for s in ("Left", "Right") for b in
               ["ForeArm", "Hand"] + [f"Hand{f}{k}" for f in FINGERS for k in (1, 2, 3)]}


LEG_TWIST = {f"{s}{b}" for s in ("Left", "Right") for b in ("UpLeg", "Leg", "Foot")}


def smooth_vectors(v, fps, cutoff=3.0, spike_deg=25.0, win=9, keep_norm=False):
    """Série de vecteurs (T,3) : directions aberrantes (écart à la médiane locale > spike_deg)
    remplacées par interpolation, puis passe-bas. Les trous (main cachée) sont comblés en
    tournant progressivement d'une direction connue à la suivante, au lieu de figer la main
    puis de la faire sauter quand elle réapparaît."""
    v = np.asarray(v, float)
    T = len(v)
    ok = np.isfinite(v).all(1) & (np.linalg.norm(np.nan_to_num(v), axis=1) > 1e-9)
    if ok.sum() < 2 * win:
        return v
    n = np.linalg.norm(v, axis=1, keepdims=True)
    u = np.where(ok[:, None], v / np.where(n > 0, n, 1), np.nan)
    idx = np.where(ok)[0]
    t = np.arange(T)
    uf = np.stack([np.interp(t, idx, u[idx, k]) for k in range(3)], 1)
    pad = win // 2
    up = np.concatenate([np.repeat(uf[:1], pad, 0), uf, np.repeat(uf[-1:], pad, 0)])
    from numpy.lib.stride_tricks import sliding_window_view
    med = np.median(sliding_window_view(up, win, axis=0), axis=-1)
    med /= np.linalg.norm(med, axis=1, keepdims=True)
    dev = np.degrees(np.arccos(np.clip(np.sum(uf * med, 1), -1, 1)))
    good = ok & (dev <= spike_deg)
    if good.sum() >= 2:
        g = np.where(good)[0]
        uf = np.stack([np.interp(t, g, uf[g, k]) for k in range(3)], 1)
    uf = lowpass(uf, fps, cutoff)
    uf /= np.linalg.norm(uf, axis=1, keepdims=True)
    if keep_norm:
        nf = np.interp(t, idx, n[idx, 0])[:, None]
        return uf * nf
    return uf


def stable_twist(dm, rm, fps, cutoff=2.5, spike_deg=35.0, win=15):
    """Référence de torsion lissée *autour de l'os*. On suit l'axe de l'os avec un repère
    transporté sans torsion (rotation minimale d'une image à l'autre), on mesure l'angle de la
    référence brute (ligne des jointures de la main) dans ce repère, et c'est cet angle qui est
    nettoyé et lissé. Lisser directement la référence dans l'espace la ferait traîner derrière
    l'os quand le bras bouge vite, jusqu'à devenir parallèle à l'os : la torsion basculerait."""
    dm = np.asarray(dm, float)
    rm = np.asarray(rm, float)
    T = len(dm)
    okd = np.isfinite(dm).all(1) & (np.linalg.norm(np.nan_to_num(dm), axis=1) > 1e-9)
    if okd.sum() < 2:
        return rm
    t = np.arange(T)
    idx = np.where(okd)[0]
    a = np.stack([np.interp(t, idx, dm[idx, k]) for k in range(3)], 1)
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    # repère transporté le long de l'axe
    p = np.empty((T, 3))
    seed = np.cross(a[0], [0.0, 0.0, 1.0])
    if np.linalg.norm(seed) < 1e-3:
        seed = np.cross(a[0], [1.0, 0.0, 0.0])
    p[0] = seed / np.linalg.norm(seed)
    for i in range(1, T):
        v = np.cross(a[i - 1], a[i])
        c = float(np.dot(a[i - 1], a[i]))
        s2 = float(np.dot(v, v))
        q = p[i - 1]
        if s2 > 1e-12:
            q = q * c + np.cross(v, q) + v * np.dot(v, q) * (1 - c) / s2
        q = q - a[i] * np.dot(q, a[i])
        p[i] = q / np.linalg.norm(q)
    w = np.cross(a, p)
    k = rm - a * np.sum(rm * a, 1, keepdims=True)
    kn = np.linalg.norm(np.nan_to_num(k), axis=1)
    ok = np.isfinite(k).all(1) & (kn > 1e-6 * (np.nanmedian(kn[kn > 0]) if (kn > 0).any() else 1.0))
    if ok.sum() < 2:
        return rm
    g = np.where(ok)[0]
    th = np.unwrap(np.arctan2(np.sum(w[g] * k[g], 1), np.sum(p[g] * k[g], 1)))
    th = np.interp(t, g, th)
    pad = win // 2
    from numpy.lib.stride_tricks import sliding_window_view
    med = np.median(sliding_window_view(np.concatenate([np.repeat(th[:1], pad), th, np.repeat(th[-1:], pad)]),
                                        win), axis=-1)
    good = ok & (np.degrees(np.abs(th - med)) <= spike_deg)
    if good.sum() >= 2:
        gg = np.where(good)[0]
        th = np.interp(t, gg, th[gg])
    th = lowpass(th[:, None], fps, cutoff)[:, 0]
    return np.cos(th)[:, None] * p + np.sin(th)[:, None] * w


def continuous_quats(q):
    q = np.array(q, float)
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    return q


def clean_rotations(q, fps, cutoff=8.0, spike_deg=20.0, win=7):
    """Rotations (T,4) d'un os : signes rendus continus, pics isolés (écart à la médiane locale
    > spike_deg) remplacés par interpolation, puis filtre passe-bas aller-retour.
    Enlève les tressautements (torsion de l'avant-bras surtout) sans retarder le mouvement."""
    q = np.array(q, float)
    T = len(q)
    if T < 2 * win:
        return q
    for i in range(1, T):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    pad = win // 2
    qp = np.concatenate([np.repeat(q[:1], pad, 0), q, np.repeat(q[-1:], pad, 0)])
    from numpy.lib.stride_tricks import sliding_window_view
    med = np.median(sliding_window_view(qp, win, axis=0), axis=-1)
    med /= np.linalg.norm(med, axis=1, keepdims=True)
    dev = np.degrees(2 * np.arccos(np.clip(np.abs(np.sum(q * med, 1)), 0, 1)))
    bad = dev > spike_deg
    if bad.any() and not bad.all():
        good = np.where(~bad)[0]
        t = np.arange(T)
        q = np.stack([np.interp(t, good, q[good, k]) for k in range(4)], 1)
    q = lowpass(q, fps, cutoff)
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def write_action(obj, name, start_frame, channels):
    ad = obj.animation_data or obj.animation_data_create()
    act = bpy.data.actions.new(name)
    ad.action = act
    frames = None
    for dp, idx, vals, group in channels:
        if hasattr(act, "fcurve_ensure_for_datablock"):      # Blender 4.4+
            fc = act.fcurve_ensure_for_datablock(obj, dp, index=idx, group_name=group)
        else:
            fc = act.fcurves.new(dp, index=idx, action_group=group)
        n = len(vals)
        if frames is None or len(frames) != n:
            frames = np.arange(start_frame, start_frame + n, dtype=float)
        fc.keyframe_points.add(n)
        co = np.empty(2 * n)
        co[0::2] = frames
        co[1::2] = vals
        fc.keyframe_points.foreach_set("co", co.tolist())
        fc.update()
    return act


# --------------------------------------------------------------------------- squelette généré

def _median_len(J, a, b, default):
    v = np.linalg.norm(J[a] - J[b], axis=-1)
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size > 10 else default


def create_mixamo_armature(context, J, name="MocapCam_Squelette", obj_matrix=None,
                           pose="T"):
    """Crée un squelette aux 65 os Mixamo ('mixamorig:'), en T-pose,
    aux proportions mesurées sur la capture. Face à -Y, gauche du perso en +X."""
    Jd = derive(dict(J))
    m = lambda a, b, d: 0.5 * (_median_len(Jd, "l_" + a, "l_" + b, d) + _median_len(Jd, "r_" + a, "r_" + b, d))
    thigh, shin = m("hip", "knee", 0.43), m("knee", "ankle", 0.42)
    upper, fore = m("shoulder", "elbow", 0.28), m("elbow", "wrist", 0.26)
    hip_w = _median_len(Jd, "l_hip", "r_hip", 0.2)
    sh_w = _median_len(Jd, "l_shoulder", "r_shoulder", 0.36)
    torso = _median_len(Jd, "mid_hip", "mid_sh", 0.5)
    neck = _median_len(Jd, "mid_sh", "mid_ear", 0.2)
    foot = m("ankle", "toes", 0.18)
    sc = (thigh + shin) / 0.85

    def hand_len(a, b, d):
        return 0.5 * (_median_len(Jd, f"lh_{a}", f"lh_{b}", d) + _median_len(Jd, f"rh_{a}", f"rh_{b}", d))
    palm = hand_len(0, 9, 0.09 * sc)

    ankle_h = 0.07 * sc
    knee_z = ankle_h + shin
    hip_z = knee_z + thigh
    sh_z = hip_z + torso
    P = {}  # os -> (tête, queue) en coordonnées monde
    up = np.array([0, 0, 1.0])
    P["Hips"] = (np.array([0, 0, hip_z]), np.array([0, 0, hip_z + 0.1 * torso]))
    P["Spine"] = (np.array([0, 0, hip_z + 0.1 * torso]), np.array([0, 0, hip_z + 0.35 * torso]))
    P["Spine1"] = (P["Spine"][1], np.array([0, 0, hip_z + 0.6 * torso]))
    P["Spine2"] = (P["Spine1"][1], np.array([0, 0, hip_z + 0.92 * torso]))
    head_z = sh_z + neck - 0.07 * sc
    P["Neck"] = (P["Spine2"][1], np.array([0, 0, head_z]))
    P["Head"] = (np.array([0, 0, head_z]), np.array([0, 0, head_z + 0.2 * sc]))
    P["HeadTop_End"] = (P["Head"][1], P["Head"][1] + 0.04 * sc * up)
    for side, sg in (("Left", 1), ("Right", -1)):
        X = np.array([sg, 0, 0.0])
        sh = np.array([sg * sh_w / 2, 0, sh_z])
        P[f"{side}Shoulder"] = (np.array([sg * 0.03 * sc, 0, sh_z - 0.03 * sc]), sh)
        el = sh + upper * X
        wr = el + fore * X
        P[f"{side}Arm"] = (sh, el)
        P[f"{side}ForeArm"] = (el, wr)
        P[f"{side}Hand"] = (wr, wr + palm * X)
        bases = {"Index": wr + X * palm * 0.95 + np.array([0, -0.022 * sc, 0]),
                 "Middle": wr + X * palm,
                 "Ring": wr + X * palm * 0.95 + np.array([0, 0.02 * sc, 0]),
                 "Pinky": wr + X * palm * 0.88 + np.array([0, 0.04 * sc, 0])}
        fb = {"Thumb": 1, "Index": 5, "Middle": 9, "Ring": 13, "Pinky": 17}
        thumb_dir = nrm(np.array([sg * 1.0, -1.0, -0.35]))
        for f in FINGERS:
            b0 = fb[f]
            if f == "Thumb":
                pt = wr + sc * np.array([sg * 0.02, -0.02, -0.01])
                d = thumb_dir
                defaults = (0.04 * sc, 0.035 * sc, 0.03 * sc)
            else:
                pt = bases[f]
                d = X
                defaults = (0.04 * sc, 0.025 * sc, 0.022 * sc)
            for k in range(3):
                L = hand_len(b0 + k, b0 + k + 1, defaults[k])
                P[f"{side}Hand{f}{k + 1}"] = (pt, pt + L * d)
                pt = pt + L * d
            P[f"{side}Hand{f}4"] = (pt, pt + 0.015 * sc * d)
        hx = sg * hip_w / 2
        P[f"{side}UpLeg"] = (np.array([hx, 0, hip_z]), np.array([hx, 0, knee_z]))
        P[f"{side}Leg"] = (np.array([hx, 0, knee_z]), np.array([hx, 0, ankle_h]))
        ball = np.array([hx, -0.72 * foot, 0.02 * sc])
        tip = np.array([hx, -foot, 0.02 * sc])
        P[f"{side}Foot"] = (np.array([hx, 0, ankle_h]), ball)
        P[f"{side}ToeBase"] = (ball, tip)
        P[f"{side}Toe_End"] = (tip, tip + np.array([0, -0.03 * sc, 0]))

    if pose == "A":   # utile pour les tests : bras baissés de 45°
        for side, sg in (("Left", 1), ("Right", -1)):
            pivot = P[f"{side}Arm"][0]
            a = math.radians(-45 * sg)
            Rm = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])
            for k, (h, t) in list(P.items()):
                if k.startswith(side) and not ("Leg" in k or "Foot" in k or "Toe" in k or "Shoulder" in k):
                    P[k] = (pivot + Rm @ (h - pivot), pivot + Rm @ (t - pivot))

    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    context.collection.objects.link(obj)
    if obj_matrix is not None:
        obj.matrix_world = obj_matrix
    inv = np.linalg.inv(np_mat(obj.matrix_world))
    to_local = lambda v: Vector((inv @ np.append(v, 1.0))[:3])
    for o in context.view_layer.objects:
        o.select_set(False)
    context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = data.edit_bones
    for bname, parent in mixamo_hierarchy():
        h, t = P[bname]
        e = eb.new(PREFIX + bname)
        e.head, e.tail = to_local(h), to_local(t)
        e.roll = 0.0
        if parent:
            e.parent = eb[PREFIX + parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    data.display_type = "STICK"
    obj.show_in_front = True
    return obj


# --------------------------------------------------------------------------- armature -> armature

def armature_motion(context, src, frame_start, frame_end, with_twist=False):
    """Points équivalents à une capture (épaules, coudes, doigts…), image par image, depuis une
    armature animée aux noms Mixamo (ex. un FBX MOCAM importé).
    with_twist=True : renvoie aussi, pour chaque os, sa référence de torsion de repos tournée
    avec l'os ; elle reste solidaire de l'os, donc jamais parallèle à lui (pas de bascule)."""
    bmap = bone_map(src)
    sc = context.scene
    back = sc.frame_current
    frames = []
    refs = {}
    if with_twist:
        rest = _bone_frames(src, bmap, False)
        Jr = derive(virtual_joints(src, bmap, posed=False))
        ref0 = {}
        with np.errstate(invalid="ignore"):
            for short, (_, rfn) in bone_specs().items():
                if short in rest:
                    r = np.asarray(rfn(Jr), float)
                    if np.isfinite(r).all() and np.linalg.norm(r) > 1e-9:
                        ref0[short] = rest[short][1].T @ r     # dans le repère de l'os au repos
        refs = {k: [] for k in ref0}
    mw = src.matrix_world
    try:
        for f in range(frame_start, frame_end + 1):
            sc.frame_set(f)
            frames.append(virtual_joints(src, bmap, posed=True))
            for short in refs:
                R = np_mat((mw @ src.pose.bones[bmap[short]].matrix).to_3x3().normalized())
                refs[short].append(R @ ref0[short])
    finally:
        sc.frame_set(back)
    J = {k: np.array([fr[k] for fr in frames]) for k in frames[0]}
    if with_twist:
        return J, {k: np.array(v) for k, v in refs.items()}
    return J


def transfer_motion(context, src, dst, root_mode="RELATIVE", start_frame=None, action_name=None):
    """Retarget : l'animation de l'armature src est appliquée à l'armature dst (noms Mixamo,
    préfixe, échelle, rotation et pose de repos quelconques, T-pose ou A-pose)."""
    if src.animation_data is None or src.animation_data.action is None:
        raise RuntimeError(f"L'armature « {src.name} » n'a pas d'animation.")
    act = src.animation_data.action
    f0, f1 = (int(round(v)) for v in act.frame_range)
    J, twist = armature_motion(context, src, f0, f1, with_twist=True)
    fps = context.scene.render.fps / context.scene.render.fps_base
    name = action_name or act.name.split("|")[-1]
    rep = apply_motion(dst, J, fps, f0 if start_frame is None else start_frame, root_mode,
                       action_name=name, clean=False, twist=twist)
    rep.update(frames=f1 - f0 + 1)
    src_fps = anim_fps(src)
    if src_fps is not None:
        dst[FPS_PROP] = src_fps
        old = set_scene_fps(context.scene, src_fps)
        rep.update(fps=src_fps, fps_was=old)
    return rep


# --------------------------------------------------------------------------- export FBX

FPS_PROP = "mocapcam_fps"


def anim_fps(obj):
    """Cadence propre de l'animation d'une armature (images/s), si connue. Elle est inscrite
    dans le FBX (propriété de l'objet) et notée à l'import : importer ensuite un autre FBX
    (un personnage Mixamo est à 30 i/s) change la cadence de la scène, pas celle-ci."""
    if obj is None:
        return None
    v = obj.get(FPS_PROP)
    if v is None and obj.animation_data and obj.animation_data.action:
        v = obj.animation_data.action.get(FPS_PROP)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def scene_fps(scene):
    return scene.render.fps / scene.render.fps_base


def set_scene_fps(scene, fps):
    """Règle la cadence de la scène ; renvoie l'ancienne si elle a changé, sinon None."""
    old = scene_fps(scene)
    if abs(old - fps) < 1e-3:
        return None
    scene.render.fps = max(1, int(round(fps)))
    scene.render.fps_base = scene.render.fps / fps
    return old


def _fmt_fps(v):
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _key_rest_pose(obj, frame):
    """Pose de repos (T-pose pour le squelette MOCAM) clé à `frame`, juste avant
    l'animation. Le FBX écrit la position par défaut de chaque os à l'image courante :
    en se plaçant sur cette image, la pose de repos du fichier est la T-pose, et
    l'animation exportée (frame_start..frame_end) n'est pas modifiée."""
    keyed = []
    for pb in obj.pose.bones:
        pb.location = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
        paths = ["location", "scale"]
        if pb.rotation_mode == "QUATERNION":
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            paths.append("rotation_quaternion")
        elif pb.rotation_mode == "AXIS_ANGLE":
            pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            paths.append("rotation_axis_angle")
        else:
            pb.rotation_euler = (0.0, 0.0, 0.0)
            paths.append("rotation_euler")
        for path in paths:
            pb.keyframe_insert(path, frame=frame, group=pb.name)
            keyed.append((pb, path))
    return keyed


def export_fbx(context, obj, filepath, rest_frame=True, meshes=()):
    """Exporte l'armature et son animation en FBX, comme une animation Mixamo
    « sans skin » : os 'mixamorig:', Y vers le haut, personnage face à +Z, sans os de bout
    ajoutés, toutes les images gardées (pas de simplification). La pose de repos du fichier
    est celle de l'armature (T-pose pour le squelette MOCAM).
    meshes : maillages du personnage à exporter avec (textures intégrées au FBX)."""
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    for m in meshes:
        m.hide_set(False)
        m.select_set(True)
    context.view_layer.objects.active = obj
    sc = context.scene
    back = sc.frame_current
    fps = anim_fps(obj)
    if fps is None and obj.animation_data and obj.animation_data.action:
        fps = scene_fps(sc)
        obj[FPS_PROP] = fps
    # le FBX convertit les images en temps avec la cadence de la scène : on exporte à la
    # cadence de l'animation, sinon elle serait ralentie ou accélérée dans le fichier
    fps_back = (sc.render.fps, sc.render.fps_base)
    if fps is not None:
        set_scene_fps(sc, fps)
    keyed = []
    f_rest = sc.frame_start - 1
    if rest_frame and obj.animation_data and obj.animation_data.action:
        keyed = _key_rest_pose(obj, f_rest)
        sc.frame_set(f_rest)
    try:
        extra = dict(path_mode="COPY", embed_textures=True, mesh_smooth_type="FACE") if meshes else {}
        bpy.ops.export_scene.fbx(
            filepath=filepath, use_selection=True, object_types={"ARMATURE", "MESH"} if meshes else {"ARMATURE"},
            **extra,
            axis_forward="-Z", axis_up="Y", apply_unit_scale=True, global_scale=1.0,
            apply_scale_options="FBX_SCALE_NONE",
            add_leaf_bones=False, primary_bone_axis="Y", secondary_bone_axis="X",
            armature_nodetype="NULL", use_armature_deform_only=False,
            bake_anim=True, bake_anim_use_all_bones=True, bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False, bake_anim_force_startend_keying=True,
            bake_anim_step=1.0, bake_anim_simplify_factor=0.0, use_custom_props=True)
    finally:
        for pb, path in keyed:
            try:
                pb.keyframe_delete(path, frame=f_rest)
            except RuntimeError:
                pass
        sc.render.fps, sc.render.fps_base = fps_back
        sc.frame_set(back)
    return filepath


def capture_to_fbx(src, dst, root_mode="RELATIVE"):
    """Sans interface (blender -b) : capture .mocap.json -> FBX d'un squelette Mixamo animé."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    context = bpy.context
    d, J = load_motion(src)
    # l'animation commence au temps 0 du FBX (image 0), comme les animations Mixamo
    _setup_scene(context, d, 0)
    take = os.path.basename(src).replace(".mocap.json", "").replace(".json", "")
    context.scene.name = take            # nom de l'animation (« take ») dans le FBX
    arm = create_mixamo_armature(context, J, name="Armature")   # même nom que chez Mixamo
    arm[FPS_PROP] = float(d["fps"])
    rep = apply_motion(arm, J, d["fps"], 0, root_mode, action_name=take)
    export_fbx(context, arm, dst)
    rep.update(frames=int(d["frame_count"]), fps=float(d["fps"]), bones=len(arm.data.bones))
    return rep


# --------------------------------------------------------------------------- personnage

CHARACTER_EXT = (".fbx", ".glb", ".gltf", ".blend")


def load_character(context, path):
    """Importe un personnage riggé (FBX, glTF/GLB ou .blend). Renvoie (armature, maillages)."""
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path, use_anim=False, ignore_leaf_bones=False,
                                 automatic_bone_orientation=False)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".blend":
        with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
            data_to.objects = list(data_from.objects)
        for o in data_to.objects:
            if o is not None and o.name not in context.scene.collection.objects:
                context.scene.collection.objects.link(o)
    else:
        raise RuntimeError(f"Format non pris en charge : {ext} (FBX, GLB/glTF ou .blend)")
    new = [o for o in bpy.data.objects if o not in before]
    arms = [o for o in new if o.type == "ARMATURE"]
    if not arms:
        raise RuntimeError("Aucun squelette (armature) dans ce fichier : le personnage doit être riggé.")
    best, err = None, None
    for a in sorted(arms, key=lambda o: -len(o.data.bones)):
        try:
            bone_map(a)
            best = a
            break
        except RuntimeError as e:
            err = e
    if best is None:
        raise err
    meshes = [o for o in new if o.type == "MESH" and (
        o.parent == best or any(md.type == "ARMATURE" and md.object == best for md in o.modifiers))]
    # pose de repos (l'animation éventuelle du fichier est ignorée)
    if best.animation_data:
        best.animation_data.action = None
    for pb in best.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    return best, meshes


def character_info(arm, meshes):
    m = bone_map(arm)
    tris = 0
    zs = []
    for o in meshes:
        me = o.data
        tris += sum(len(p.vertices) - 2 for p in me.polygons)
        zs += [(o.matrix_world @ Vector(c)).z for c in o.bound_box]
    if not zs:
        zs = [(arm.matrix_world @ b.head_local).z for b in arm.data.bones]
    return dict(armature=arm.name, kind=rig_kind(arm), bones=len(arm.data.bones), mapped=len(m),
                fingers=sum(1 for k in m if "Hand" in k and k[-1].isdigit()), meshes=len(meshes),
                triangles=tris, height=round(max(zs) - min(zs), 3) if zs else None,
                arkit=len(face_shapes(meshes)))


def export_glb(context, objs, filepath, animated=True):
    """GLB pour l'aperçu 3D de MOCAM Studio (navigateur)."""
    for o in context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.hide_set(False)
        o.select_set(True)
    context.view_layer.objects.active = objs[0]
    kw = dict(filepath=filepath, export_format="GLB", use_selection=True, export_animations=animated,
              export_skins=True, export_yup=True)
    try:
        bpy.ops.export_scene.gltf(**kw, export_animation_mode="ACTIVE_ACTIONS", export_force_sampling=True)
    except (TypeError, RuntimeError):
        bpy.ops.export_scene.gltf(**kw)
    return filepath


def inspect_character(path, glb_path=None):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        arm, meshes = load_character(bpy.context, path)
        info = character_info(arm, meshes)
        objs = [arm] + meshes
    except RuntimeError as err:
        # tête seule à blendshapes ARKit (sans squelette humanoïde) : utilisable pour le visage
        arm, meshes = _load_any(bpy.context, path)
        n = len(face_shapes(meshes))
        if not n:
            raise err
        tris = sum(len(p.vertices) - 2 for o in meshes for p in o.data.polygons)
        info = dict(armature=arm.name if arm else None, kind="Visage seul", bones=len(arm.data.bones) if arm else 0,
                    mapped=0, fingers=0, meshes=len(meshes), triangles=tris, height=None, arkit=n, face_only=True)
        objs = ([arm] if arm else []) + meshes
    if glb_path:
        export_glb(bpy.context, objs, glb_path, animated=False)
    return info


def retarget_file(anim_path, char_path, out_fbx, root_mode="RELATIVE", glb_path=None):
    """Animation FBX (MOCAM ou Mixamo) -> personnage riggé animé (FBX avec maillage)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    context = bpy.context
    dst, meshes = load_character(context, char_path)
    src = import_animation(context, anim_path)
    take = src.name
    rep = transfer_motion(context, src, dst, root_mode, action_name=take)
    for o in [src] + list(src.children_recursive):
        bpy.data.objects.remove(o, do_unlink=True)
    act = dst.animation_data.action
    sc = context.scene
    sc.frame_start, sc.frame_end = (int(round(v)) for v in act.frame_range)
    sc.name = take
    export_fbx(context, dst, out_fbx, meshes=meshes)
    if glb_path:
        fps = anim_fps(dst)
        if fps:
            set_scene_fps(sc, fps)
        export_glb(context, [dst] + meshes, glb_path, animated=True)
    rep.update(character=character_info(dst, meshes), fbx=out_fbx, glb=glb_path)
    return rep


# --------------------------------------------------------------------------- capture faciale
# Visage MOCAM : maillage appris sur la prise (468 sommets) avec les 52 blendshapes ARKit,
# posé sur un os de tête ; ou transfert des courbes sur les blendshapes d'un personnage.

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


def arkit_key(name):
    """Nom de blendshape -> nom ARKit normalisé, ou None. Accepte « eyeBlinkLeft », « EyeBlink_L »,
    « A01_Brow_Inner_Up », « blendShape1.jawOpen », « eye_blink_left »…"""
    import re as _re
    n = name.split(".")[-1].split(":")[-1]
    n = _re.sub(r"^[A-Za-z]\d\d_", "", n)
    n = _re.sub(r"(?:[\s_\-](?:L|l)|[\s_\-]?(?:Left|left))$", "Left", n)
    n = _re.sub(r"(?:[\s_\-](?:R|r)|[\s_\-]?(?:Right|right))$", "Right", n)
    k = _re.sub(r"[^a-z]", "", n.lower())
    return _ARKIT_NORM.get(k)


_ARKIT_NORM = {n.lower(): n for n in ARKIT}


def _face_curves(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d, np.array(d["weights"], float), np.array(d["head"], float)


def _head_quat(deg):
    """Rotation de la tête (lacet, tangage, roulis en degrés, repère caméra) -> quaternion
    dans le repère Blender d'un personnage qui regarde vers -Y."""
    from mathutils import Euler
    yaw, pitch, roll = [math.radians(v) for v in deg]
    # caméra : x -> +X, y (haut) -> +Z, z (vers la caméra) -> -Y
    return Euler((pitch, roll, yaw), "YXZ").to_quaternion()


def _key_face(key_blocks_by_arkit, W, start=0):
    n = 0
    for name, blocks in key_blocks_by_arkit.items():
        j = ARKIT.index(name)
        for kb in blocks:
            kb.value = 0.0
            for i in range(len(W)):
                kb.value = float(W[i, j])
                kb.keyframe_insert("value", frame=start + i)
            n += 1
    return n


def face_file(json_path, fbx_path, glb_path=None):
    """Visage MOCAM animé : maillage + 52 blendshapes ARKit + os « Head », en FBX (et GLB)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    context = bpy.context
    d, W, H = _face_curves(json_path)
    fps = float(d["fps"])
    sc = context.scene
    set_scene_fps(sc, fps)
    sc.frame_start, sc.frame_end = 0, len(W) - 1
    take = os.path.splitext(os.path.basename(json_path))[0]
    sc.name = take
    face = d["face"]
    V = np.array(face["vertices"], float)
    D = np.array(face["deltas"], float)
    # armature : un os de tête sous le menton
    arm_data = bpy.data.armatures.new("MOCAM_Tete")
    arm = bpy.data.objects.new("MOCAM_Tete", arm_data)
    sc.collection.objects.link(arm)
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones.new("Head")
    eb.head = (0.0, 0.09, -0.03)       # pivot sous le crâne, derrière le visage
    eb.tail = (0.0, 0.09, 0.18)
    bpy.ops.object.mode_set(mode="OBJECT")
    me = bpy.data.meshes.new("MOCAM_Visage")
    me.from_pydata([tuple(v) for v in V], [], [tuple(t) for t in face["triangles"]])
    me.update()
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    mat = bpy.data.materials.new("MOCAM_Peau")
    mat.diffuse_color = (0.78, 0.66, 0.6, 1)
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.78, 0.66, 0.6, 1)
        bsdf.inputs["Roughness"].default_value = 0.6
    me.materials.append(mat)
    ob = bpy.data.objects.new("MOCAM_Visage", me)
    sc.collection.objects.link(ob)
    ob.parent = arm
    ob.vertex_groups.new(name="Head").add(list(range(len(V))), 1.0, "REPLACE")
    ob.modifiers.new("Armature", "ARMATURE").object = arm
    ob.shape_key_add(name="Basis", from_mix=False)
    blocks = {}
    for j, name in enumerate(ARKIT):
        kb = ob.shape_key_add(name=name, from_mix=False)
        kb.data.foreach_set("co", (V + D[j]).ravel().tolist())
        kb.slider_min, kb.slider_max = 0.0, 1.0
        blocks[name] = [kb]
    n_keys = _key_face(blocks, W)
    if me.shape_keys.animation_data and me.shape_keys.animation_data.action:
        me.shape_keys.animation_data.action.name = take + "_visage"
    pb = arm.pose.bones["Head"]
    pb.rotation_mode = "QUATERNION"
    for i in range(len(H)):
        pb.rotation_quaternion = _head_quat(H[i])
        pb.keyframe_insert("rotation_quaternion", frame=i)
    if arm.animation_data and arm.animation_data.action:
        arm.animation_data.action.name = take
    arm[FPS_PROP] = fps
    export_fbx(context, arm, fbx_path, rest_frame=False, meshes=[ob])
    if glb_path:
        os.makedirs(os.path.dirname(glb_path), exist_ok=True)
        export_glb(context, [arm, ob], glb_path, animated=True)
    return dict(frames=len(W), fps=fps, shapes=n_keys, vertices=len(V))


def _load_any(context, path):
    """Importe un fichier de personnage, avec ou sans squelette. Renvoie (armature|None, maillages)."""
    try:
        return load_character(context, path)
    except RuntimeError:
        pass
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path, use_anim=False)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".blend":
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for o in dst.objects:
            if o is not None:
                context.scene.collection.objects.link(o)
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    return (arms[0] if arms else None), [o for o in bpy.data.objects if o.type == "MESH"]


def face_shapes(meshes):
    """Blendshapes ARKit trouvées sur les maillages : {nom ARKit: [key blocks]}."""
    found = {}
    for o in meshes:
        keys = o.data.shape_keys
        if not keys:
            continue
        for kb in keys.key_blocks[1:]:
            k = arkit_key(kb.name)
            if k:
                found.setdefault(k, []).append(kb)
    return found


def main_cli():
    """blender -b --factory-startup --python <ce fichier> -- …
      capture.mocap.json sortie.fbx [RELATIVE|ABSOLUTE]                capture -> rig Mixamo animé
      retarget anim.fbx personnage.fbx sortie.fbx [RELATIVE|ABSOLUTE] [apercu.glb]
      inspect personnage.fbx [apercu.glb]"""
    import sys
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if argv and argv[0] == "retarget":
        if len(argv) < 4:
            raise SystemExit("usage : -- retarget anim.fbx personnage sortie.fbx [RELATIVE|ABSOLUTE] [apercu.glb]")
        rep = retarget_file(argv[1], argv[2], argv[3], argv[4] if len(argv) > 4 else "RELATIVE",
                            argv[5] if len(argv) > 5 else None)
        print(f"MOCAPCAM_RETARGET_OK {json.dumps(rep, default=str)}")
        return
    if argv and argv[0] == "face":
        rep = face_file(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
        print(f"MOCAPCAM_FACE_OK {json.dumps(rep)}")
        return
    if argv and argv[0] == "inspect":
        info = inspect_character(argv[1], argv[2] if len(argv) > 2 else None)
        print(f"MOCAPCAM_INSPECT_OK {json.dumps(info)}")
        return
    if len(argv) < 2:
        raise SystemExit("usage : -- capture.mocap.json sortie.fbx [RELATIVE|ABSOLUTE]")
    rep = capture_to_fbx(argv[0], argv[1], argv[2] if len(argv) > 2 else "RELATIVE")
    print(f"MOCAPCAM_FBX_OK {json.dumps(rep, default=str)}")


# --------------------------------------------------------------------------- import

def _setup_scene(context, d, start):
    sc = context.scene
    sc.render.fps = int(round(d["fps"]))
    sc.render.fps_base = 1.0
    sc.frame_start = start
    sc.frame_end = start + d["frame_count"] - 1


def import_animation(context, filepath):
    """Importe un FBX d'animation (MOCAM ou Mixamo) ; renvoie l'armature animée."""
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=filepath, use_anim=True, ignore_leaf_bones=False,
                             automatic_bone_orientation=False)
    new = [o for o in bpy.data.objects if o not in before]
    arms = [o for o in new if o.type == "ARMATURE"]
    if not arms:
        raise RuntimeError("Aucune armature dans ce FBX.")
    arm = max(arms, key=lambda o: len(o.data.bones))
    bone_map(arm)                                   # vérifie que le squelette est reconnu
    arm.name = os.path.splitext(os.path.basename(filepath))[0]
    if anim_fps(arm) is None:
        arm[FPS_PROP] = scene_fps(context.scene)
    else:
        set_scene_fps(context.scene, anim_fps(arm))
    ad = arm.animation_data
    if ad and ad.action:
        f0, f1 = (int(round(v)) for v in ad.action.frame_range)
        context.scene.frame_start, context.scene.frame_end = f0, f1
        ad.action.use_fake_user = True
    return arm


def rig_kind(obj):
    """Nom lisible du type de squelette (Mixamo, Unreal…)."""
    names = [b.name for b in obj.data.bones]
    try:
        find_prefix(obj)
        if any(n.endswith("LeftArm") for n in names):
            return "Mixamo"
    except RuntimeError:
        pass
    j = " ".join(names)
    for key, label in (("CC_Base_", "Character Creator"), ("Bip0", "Biped (3ds Max)"), ("J_Bip_", "VRoid"),
                       ("DEF-spine", "Rigify"), ("upperarm_l", "Unreal"), ("lShldr", "DAZ")):
        if key in j:
            return label
    return "autre"


if __name__ == "__main__":
    main_cli()
