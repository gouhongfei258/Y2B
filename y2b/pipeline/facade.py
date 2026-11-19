#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""运行期回查 ``y2b.pipeline.task_manager`` 上的名字。

拆出去的模块不在导入时引用 task_manager，避免循环导入。调用发生在
task_manager 完成初始化之后，因此打在公开模块上的 monkeypatch 仍然生效。
"""

import os


class Live:
    """在每次使用时读取 task_manager 上的同名对象。"""

    def __init__(self, name):
        object.__setattr__(self, '_name', name)

    def _resolve(self):
        import y2b.pipeline.task_manager as task_manager
        return getattr(task_manager, self._name)

    def __call__(self, *args, **kwargs):
        return self._resolve()(*args, **kwargs)

    def __getitem__(self, key):
        return self._resolve()[key]

    def __setitem__(self, key, value):
        self._resolve()[key] = value

    def __getattr__(self, attr):
        return getattr(self._resolve(), attr)

    def __fspath__(self):
        return os.fspath(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __ne__(self, other):
        return self._resolve() != other

    def __hash__(self):
        return hash(self._resolve())

    def __bool__(self):
        return bool(self._resolve())

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return repr(self._resolve())

    def __format__(self, spec):
        return format(self._resolve(), spec)

    def __iter__(self):
        return iter(self._resolve())

    def __len__(self):
        return len(self._resolve())

    def __contains__(self, item):
        return item in self._resolve()
