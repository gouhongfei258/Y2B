"""Compatibility shim for y2b.bilibili.bili_sdk.exceptions.NetworkException."""
import importlib
import sys

_TARGET = "y2b.bilibili.bili_sdk.exceptions.NetworkException"

if __name__ == "__main__":
    import runpy
    runpy.run_module(_TARGET, run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module(_TARGET)
