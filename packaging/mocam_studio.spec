# PyInstaller : MOCAM Studio.exe (dossier autonome, Python et bibliothèques compris).
#   pyinstaller packaging/mocam_studio.spec --noconfirm   ->   dist/MOCAM Studio/
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(ROOT, "mocapcam", "studio_ui"), os.path.join("mocapcam", "studio_ui")),
    (os.path.join(ROOT, "mocapcam", "blender_script.py"), "mocapcam"),   # lancé par Blender
]
binaries = []
hiddenimports = collect_submodules("mocapcam")
for pkg in ("mediapipe", "rtmlib", "onnxruntime", "imageio_ffmpeg", "segno"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

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
