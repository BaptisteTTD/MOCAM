"""Emplacements de MOCAM, en développement (python -m mocapcam) comme dans MOCAM Studio.exe.

ROOT : dossier des données (studio, captures, visages, modeles, personnages_fournis) ; c'est le
dossier du dépôt, ou celui de MOCAM Studio.exe une fois empaqueté.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(PKG)


def self_command():
    """Commande qui relance MOCAM en ligne de commande (traitements en arrière-plan)."""
    return [sys.executable] if FROZEN else [sys.executable, "-u", "-m", "mocapcam"]


def system_language():
    """« fr » si Windows (ou le système) est en français, « en » sinon. MOCAM_LANG l'impose."""
    forced = os.environ.get("MOCAM_LANG", "").lower()
    if forced in ("fr", "en"):
        return forced
    try:
        if sys.platform.startswith("win"):
            import ctypes
            return "fr" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0C else "en"
        import locale
        loc = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
        return "fr" if loc.startswith("fr") else "en"
    except Exception:  # noqa: BLE001
        return "fr"


def say(fr, en):
    """Texte de la fenêtre de MOCAM Studio dans la langue du système."""
    return fr if system_language() == "fr" else en
