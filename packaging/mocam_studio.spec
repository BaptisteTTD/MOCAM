# PyInstaller : MOCAM Studio.exe (dossier autonome, Python et bibliothèques compris).
#   pyinstaller packaging/mocam_studio.spec --noconfirm   ->   dist/MOCAM Studio/
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(ROOT, "mocapcam", "studio_ui"), os.path.join("mocapcam", "studio_ui")),
    (os.path.join(ROOT, "mocapcam", "blender_script.py"), "mocapcam"),   # lancé par Blender
    (os.path.join(ROOT, "mocapcam", "donnees"), os.path.join("mocapcam", "donnees")),
]
# Blender intégré (module bpy, ~1 Go une fois installé) : copié tel quel, sans analyse, pour que
# MOCAM Studio.exe crée les FBX sans que Blender soit installé sur le PC
import importlib.util
_bpy = importlib.util.find_spec("bpy")
if _bpy is None:
    raise SystemExit("installez le module bpy (pip install bpy, Python 3.13) avant de construire l'exe")
_bpy_dir = list(_bpy.submodule_search_locations)[0]
# sans le moteur de rendu Cycles ni ses noyaux GPU (inutiles pour créer des FBX) : ~300 Mo de moins
_drop = ("addons_core/cycles", "cycles_kernel", "OpenImageDenoise_device", "templates_py", "templates_osl")
for _dp, _dn, _fn in os.walk(_bpy_dir):
    _rel = os.path.relpath(_dp, _bpy_dir).replace(os.sep, "/")
    if any(k in _rel + "/" for k in _drop):
        continue
    for _f in _fn:
        if any(k in _f for k in _drop):
            continue
        datas.append((os.path.join(_dp, _f), os.path.normpath(os.path.join("bpy", _rel))))
binaries = []
hiddenimports = collect_submodules("mocapcam")
for pkg in ("mediapipe", "rtmlib", "onnxruntime", "imageio_ffmpeg", "segno",
            "requests", "zstandard", "cattrs"):      # (les trois derniers : pour bpy)
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# bpy (copié sans analyse) et ses exportateurs FBX/glTF utilisent toute la bibliothèque standard
import sys
_skip = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "ensurepip", "lib2to3", "antigravity",
         "this", "pydoc_data", "venv"}
for _m in sorted(sys.stdlib_module_names):
    if _m in _skip or _m.startswith("_"):
        continue
    try:
        hiddenimports += collect_submodules(_m, filter=lambda n: ".test" not in n and "idle" not in n)
    except Exception:
        pass

a = Analysis(
    [os.path.join(SPECPATH, "mocam_studio.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "jupyter", "pytest", "torch", "tensorflow", "bpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MOCAM Studio",
    icon=os.path.join(ROOT, "mocapcam", "studio_ui", "mocam.ico"),
    console=True,          # fenêtre du Studio (à laisser ouverte), comme MOCAM Studio.bat
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MOCAM Studio", upx=False)
