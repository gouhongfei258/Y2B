#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务取消标记与活动线程登记。

这些状态在进程内只有一份。task_manager 再导出同名对象，调用方和测试继续从
modules.task_manager 访问。
"""

import threading


class TaskCancelledError(Exception):
    """任务取消异常，用于中断执行流程"""


_TASK_CANCEL_FLAGS = {}
_TASK_CANCEL_LOCK = threading.Lock()
_ACTIVE_TASK_IDS = set()
_ACTIVE_TASKS_LOCK = threading.Lock()
_TASK_SCHEDULING_LOCK = threading.RLock()
# 全局 TaskProcessor 的创建、刷新和关闭必须串行，避免并发初始化出多个 scheduler。
_GLOBAL_TASK_PROCESSOR_LOCK = threading.RLock()


def get_task_cancel_event(task_id):
    """获取任务取消事件（若不存在则创建）"""
    if not task_id:
        return None
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        if not event:
            event = threading.Event()
            _TASK_CANCEL_FLAGS[task_id] = event
        return event


def request_task_cancel(task_id):
    """请求取消任务，用于快速终止运行中的任务"""
    event = get_task_cancel_event(task_id)
    if event:
        event.set()
    return event


def is_task_cancelled(task_id):
    """检查任务是否已被请求取消"""
    if not task_id:
        return False
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        return bool(event and event.is_set())


def clear_task_cancel(task_id, clear_flag=False):
    """清理任务取消标记"""
    if not task_id or not clear_flag:
        return
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_FLAGS.pop(task_id, None)


def _mark_task_active(task_id: str) -> bool:
    """标记任务线程已激活，返回 False 表示同任务线程已存在。"""
    if not task_id:
        return False
    with _ACTIVE_TASKS_LOCK:
        if task_id in _ACTIVE_TASK_IDS:
            return False
        _ACTIVE_TASK_IDS.add(task_id)
        return True


def _mark_task_inactive(task_id: str) -> None:
    if not task_id:
        return
    with _ACTIVE_TASKS_LOCK:
        _ACTIVE_TASK_IDS.discard(task_id)


def _is_task_active(task_id: str) -> bool:
    if not task_id:
        return False
    with _ACTIVE_TASKS_LOCK:
        return task_id in _ACTIVE_TASK_IDS


def _get_active_task_ids() -> set:
    with _ACTIVE_TASKS_LOCK:
        return set(_ACTIVE_TASK_IDS)


def _raise_if_cancelled(task_id, task_logger=None):
    if is_task_cancelled(task_id):
        if task_logger:
            task_logger.info("检测到任务取消请求，终止任务执行")
        raise TaskCancelledError("任务已取消")
