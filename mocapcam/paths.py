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
