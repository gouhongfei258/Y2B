"""Compatibility shim for y2b.bilibili.bili_sdk.clients.

This package object stays here so its submodule shims can load.
Attributes are re-exported from the implementation package.
"""
import importlib

_impl = importlib.import_module("y2b.bilibili.bili_sdk.clients")
globals().update({
    _name: getattr(_impl, _name)
    for _name in dir(_impl)
    if not _name.startswith("__")
})
