"""Ligne de commande MOCAM.

  python -m mocapcam init        STUDIO
  python -m mocapcam intrinseques STUDIO CAMERA VIDEO_DAMIER
  python -m mocapcam marqueurs   STUDIO DOSSIER_PRISE
  python -m mocapcam traiter     STUDIO DOSSIER_PRISE [--force] [--decalage poco=0.12]
  python -m mocapcam fbx         DOSSIER_PRISE [--blender CHEMIN] [--absolu]
  python -m mocapcam taille      DOSSIER_PRISE TAILLE_CM      (prise déjà traitée)
  python -m mocapcam animer      STUDIO DOSSIER_PRISE PERSONNAGE  (personnage de studio/personnages)
  python -m mocapcam visage      DOSSIER_VISAGE [--studio S] [--retraiter]   (capture faciale)
  python -m mocapcam diagnostic  vérifie les modèles, ffmpeg et Blender
"""
import argparse
import os
import sys

from . import intrinsics, pipeline, video
from .paths import ROOT


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mocapcam", description="Motion capture multi-téléphones")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="crée le dossier studio (studio.json)")
    p.add_argument("studio")

    p = sub.add_parser("studio", help="interface de pilotage des téléphones")
    p.add_argument("--studio", default=os.path.join(ROOT, "studio"))
    p.add_argument("--captures", default=os.path.join(ROOT, "captures"))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--sans-fenetre", action="store_true", help="ne pas ouvrir l'interface")

    p = sub.add_parser("damier", help="(optionnel) crée l'image damier.png")
    p.add_argument("studio")

    p = sub.add_parser("intrinseques", help="(optionnel) calibration optique au damier")
    p.add_argument("studio")
    p.add_argument("camera")
    p.add_argument("video")

    for name, hlp in (("marqueurs", "vérifie la détection des boules et la position des caméras"),
                      ("traiter", "traite une prise complète -> prise.fbx (squelette Mixamo animé)")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("studio")
        p.add_argument("prise")
        p.add_argument("--force", action="store_true", help="refaire tous les calculs")
        p.add_argument("--decalage", action="append", default=[],
                       help="décalage manuel en secondes, ex. poco=0.120")

    p = sub.add_parser("fbx", help="crée le FBX (squelette Mixamo animé) d'une prise traitée")
    p.add_argument("capture", help="dossier de la prise (déjà traitée)")
    p.add_argument("--blender", help="chemin de blender.exe (sinon recherche automatique)")
    p.add_argument("--absolu", action="store_true",
                   help="garder la position dans la zone de capture (sinon le perso part de l'origine)")

    p = sub.add_parser("taille", help="met une prise traitée à la taille de l'acteur (cm), recrée le FBX")
    p.add_argument("capture", help="dossier de la prise (déjà traitée)")
    p.add_argument("cm", type=float, help="taille de l'acteur en cm, ex. 178")
    p.add_argument("--studio", default=os.path.join(ROOT, "studio"))
    p.add_argument("--blender", help="chemin de blender.exe (sinon studio.json ou recherche automatique)")

    p = sub.add_parser("animer", help="anime un personnage riggé avec une prise traitée")
    p.add_argument("studio")
    p.add_argument("capture")
    p.add_argument("personnage", help="identifiant du personnage (dossier de studio/personnages)")
    p.add_argument("--blender", help="chemin de blender.exe (sinon studio.json ou recherche automatique)")

    p = sub.add_parser("visage", help="capture faciale : vidéo d'un visage -> 52 blendshapes ARKit (FBX)")
    p.add_argument("dossier", help="dossier de la prise de visage (avec sa vidéo)")
    p.add_argument("--studio", default=os.path.join(ROOT, "studio"))
    p.add_argument("--blender")
    p.add_argument("--retraiter", action="store_true", help="refaire le suivi du visage")

    sub.add_parser("diagnostic", help="vérifie que tout est prêt (modèles, ffmpeg, Blender)")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "studio":
            from . import studio_server
            studio_server.run(a.studio, a.captures, a.port, open_ui=not a.sans_fenetre)
        elif a.cmd == "fbx":
            from . import fbx
            src = a.capture
            if os.path.isdir(src):
                found = [os.path.join(d, f) for d in (os.path.join(src, "mocapcam"), src) if os.path.isdir(d)
                         for f in os.listdir(d) if f.endswith(".mocap.json")]
                if not found:
                    raise FileNotFoundError(f"Prise non traitée : {src}. Traitez-la d'abord.")
                src = found[0]
            if not fbx.export(src, blender=a.blender, root_mode="ABSOLUTE" if a.absolu else "RELATIVE"):
                return 1
        elif a.cmd == "taille":
            pipeline.rescale_take(a.capture, a.cm, blender=a.blender,
                                  studio_dir=a.studio if os.path.isdir(a.studio) else None)
        elif a.cmd == "animer":
            pipeline.animate_character(a.capture, a.studio, a.personnage, blender=a.blender)
        elif a.cmd == "visage":
            from . import face
            st = a.studio if os.path.isdir(a.studio) else None
            bl = a.blender or ((pipeline.load_studio(st) or {}).get("blender") if st else None)
            face.run(a.dossier, studio_dir=st, blender=bl, retrack=a.retraiter)
        elif a.cmd == "diagnostic":
            return diagnostic()
        elif a.cmd == "init":
            path = pipeline.init_studio(a.studio)
            print(f"Studio créé : {path}\nMesurez la position de vos boules et éditez ce fichier.")
        elif a.cmd == "damier":
            os.makedirs(a.studio, exist_ok=True)
            print(intrinsics.make_checkerboard(os.path.join(a.studio, "damier.png")))
        elif a.cmd == "intrinseques":
            cfg = pipeline.load_studio(a.studio)
            tmp = os.path.join(a.studio, "intrinseques", f"_norm_{a.camera}.mp4")
            print("Normalisation de la vidéo…")
            video.normalize(a.video, tmp, cfg["fps"], cfg["max_height"])
            print("Recherche du damier…")
            data = intrinsics.calibrate_from_video(tmp)
            data["camera"] = a.camera
            out = os.path.join(a.studio, "intrinseques", f"{a.camera}.json")
            intrinsics.save(out, data)
            print(f"Enregistré : {out}")
        else:
            manual = {}
            for d in a.decalage:
                k, v = d.split("=")
                manual[k.strip()] = float(v)
            pipeline.process_take(a.studio, a.prise, force=a.force, manual_delays=manual,
                                  only_calib=(a.cmd == "marqueurs"))
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"\nERREUR : {e}", file=sys.stderr)
        return 1
    return 0


def diagnostic():
    """Vérifie chaque brique : détection du corps (RTMW), du visage (MediaPipe), ffmpeg, Blender."""
    import numpy as np
    from . import fbx, face
    bad = 0

    def step(label, fn):
        nonlocal bad
        try:
            print(f"[OK] {label} : {fn()}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"[ÉCHEC] {label} : {type(e).__name__} : {e}")
    step("ffmpeg", video.ffmpeg_exe)

    def body():
        from rtmlib import Wholebody
        m = Wholebody(mode="balanced", backend="onnxruntime", device="cpu", to_openpose=False)
        k, _ = m(np.zeros((480, 640, 3), np.uint8))
        return f"modèle chargé, {np.asarray(k).shape[-2] if k is not None and len(k) else 0} points"
    step("détection du corps (RTMW)", body)

    def visage():
        mp, BaseOptions, fl, Mode = face._mediapipe()
        opts = fl.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=face.ensure_model()),
                                        running_mode=Mode.IMAGE, output_face_blendshapes=True)
        with fl.FaceLandmarker.create_from_options(opts) as det:
            det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((256, 256, 3), np.uint8)))
        return "modèle chargé"
    step("détection du visage (MediaPipe)", visage)
    b = fbx.find_blender()
    print(f"[{'OK' if b else 'MANQUE'}] Blender : {b or 'introuvable (installez-le depuis blender.org)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
