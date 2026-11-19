"""Compatibility shim for y2b.subtitles.subtitle_pipeline_types."""
import importlib
import sys

_TARGET = "y2b.subtitles.subtitle_pipeline_types"

if __name__ == "__main__":
    import runpy
    runpy.run_module(_TARGET, run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module(_TARGET)
