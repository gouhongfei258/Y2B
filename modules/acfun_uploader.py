"""Compatibility shim for y2b.acfun.acfun_uploader."""
import importlib
import sys

_TARGET = "y2b.acfun.acfun_uploader"

if __name__ == "__main__":
    import runpy
    runpy.run_module(_TARGET, run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module(_TARGET)
