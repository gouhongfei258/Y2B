#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务状态变更的进程内广播。"""

import threading
import queue
from datetime import datetime, timezone

# 实时任务事件订阅管理
_TASK_EVENT_SUBSCRIBERS = set()
_TASK_EVENT_LOCK = threading.Lock()
_TASK_EVENT_QUEUE_SIZE = 128  # 增加队列容量，防止事件丢失


def register_task_updates_listener():
    """注册一个实时任务事件监听队列。"""
    listener_queue = queue.Queue(maxsize=_TASK_EVENT_QUEUE_SIZE)
    with _TASK_EVENT_LOCK:
        _TASK_EVENT_SUBSCRIBERS.add(listener_queue)
    return listener_queue


def unregister_task_updates_listener(listener_queue):
    """移除实时任务事件监听队列。"""
    with _TASK_EVENT_LOCK:
        _TASK_EVENT_SUBSCRIBERS.discard(listener_queue)


def _emit_task_event(event):
    with _TASK_EVENT_LOCK:
        subscribers = list(_TASK_EVENT_SUBSCRIBERS)

    for listener in subscribers:
        try:
            listener.put_nowait(event)
        except queue.Full:
            try:
                listener.get_nowait()
            except queue.Empty:
                pass
            try:
                listener.put_nowait(event)
            except queue.Full:
                continue


def publish_task_event(event_type='refresh', data=None):
    """向所有监听者广播任务事件。"""
    event_payload = {
        'type': event_type,
        'data': data or {},
        'timestamp': datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    }
    _emit_task_event(event_payload)
