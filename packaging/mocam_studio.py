"""Point d'entrée de MOCAM Studio.exe.

Sans argument : ouvre MOCAM Studio. Avec des arguments : la ligne de commande MOCAM
(c'est ainsi que le Studio lance ses traitements en arrière-plan).
"""
import multiprocessing
import sys


def _main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    from mocapcam.__main__ import main
    sys.exit(main(sys.argv[1:] or ["studio"]))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _main()
