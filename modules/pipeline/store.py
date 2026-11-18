#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务 SQLite 存储。

只包含连接、初始化和 tasks 表的读写。调度、上传、字幕和转码不在这里。
跨模块协作通过 _public() 回到 modules.task_manager，这样既有的
``patch.object(task_manager, "get_db_connection")`` 仍然作用在真实调用上。
"""

import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime

from modules.notifications import (
    EVENT_TASK_ADDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    NotificationEvent,
    emit_notification_event,
)
from modules.pipeline.cancel import (
    _TASK_CANCEL_FLAGS,
    _TASK_CANCEL_LOCK,
    request_task_cancel,
)
from modules.pipeline.events import publish_task_event
from modules.utils import get_app_subdir

logger = logging.getLogger("task_manager")

DB_DIR = get_app_subdir('db')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'tasks.db')
DB_CONNECT_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 30000
DB_WRITE_RETRY_TIMES = 5
DB_WRITE_RETRY_SLEEP_SECONDS = 0.2


def _public():
    """返回 task_manager 门面。只在函数执行时导入，避免循环依赖。"""
    import modules.task_manager as task_manager
    return task_manager


# 数据库操作
def init_db():
    """初始化数据库，创建tasks表"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_CONNECT_TIMEOUT_SECONDS)
    try:
        conn.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
    except Exception:
        pass
    cursor = conn.cursor()
    
    # 创建tasks表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        youtube_url TEXT NOT NULL,
        upload_target TEXT DEFAULT 'acfun',
        status TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        video_title_original TEXT,
        video_title_translated TEXT,
        description_original TEXT,
        description_translated TEXT,
        tags_generated TEXT,  -- JSON list
        recommended_partition_id TEXT,
        selected_partition_id TEXT,
        recommended_partition_id_acfun TEXT,
        selected_partition_id_acfun TEXT,
        recommended_partition_id_bilibili TEXT,
        selected_partition_id_bilibili TEXT,
        cover_path_local TEXT,
        video_path_local TEXT,
        subtitle_path_original TEXT,  -- 原始字幕文件路径
        subtitle_path_translated TEXT,  -- 翻译后字幕文件路径
        subtitle_language_detected TEXT,  -- 检测到的字幕语言
        subtitle_qc_failed INTEGER DEFAULT 0,  -- 字幕质检是否失败（1=失败）
        subtitle_qc_reason TEXT,  -- 字幕质检失败原因（可选）
        subtitle_qc_score REAL,  -- 字幕质检评分（可选）
        subtitle_qc_checked_at TIMESTAMP,  -- 最近一次实际执行字幕质检的时间（可选）
        metadata_json_path_local TEXT,
        moderation_result TEXT,  -- JSON
        error_message TEXT,
        error_category TEXT,
        pipeline_checkpoint TEXT,  -- JSON: 断点续跑已完成阶段
        upload_progress TEXT,  -- 上传进度
        acfun_upload_response TEXT,
        bilibili_upload_response TEXT,
        asr_warning_message TEXT,  -- ASR/VAD阶段的非致命警告（如vad_low_coverage），不影响上传流程
        subtitle_warning_message TEXT,  -- 字幕处理阶段的非致命警告（如烧录失败），不影响上传流程
        subtitle_quality_state TEXT  -- ASR质量结局（ok/degraded/failed），failed时禁止烧录字幕
    )
    ''')
    
    conn.commit()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS schema_migrations (
        migration_key TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # 检查并添加新字段（用于数据库升级）
    try:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'upload_progress' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_progress TEXT")
            logger.info("数据库升级：添加upload_progress字段")
            conn.commit()

        if 'subtitle_qc_failed' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_failed INTEGER DEFAULT 0")
            logger.info("数据库升级：添加subtitle_qc_failed字段")
            conn.commit()

        if 'subtitle_qc_reason' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_reason TEXT")
            logger.info("数据库升级：添加subtitle_qc_reason字段")
            conn.commit()

        if 'subtitle_qc_score' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_score REAL")
            logger.info("数据库升级：添加subtitle_qc_score字段")
            conn.commit()

        if 'subtitle_qc_checked_at' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_checked_at TIMESTAMP")
            logger.info("数据库升级：添加subtitle_qc_checked_at字段")
            conn.commit()

        if 'pipeline_checkpoint' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN pipeline_checkpoint TEXT")
            logger.info("数据库升级：添加pipeline_checkpoint字段")
            conn.commit()

        if 'error_category' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN error_category TEXT")
            cursor.execute(
                """
                UPDATE tasks
                SET error_category = ?
                WHERE status = ?
                  AND error_category IS NULL
                  AND (
                    error_message LIKE '自动翻译失败%'
                    OR error_message LIKE '自动翻译未完成%'
                  )
                """,
                (_public().METADATA_TRANSLATION_ERROR_CATEGORY, _public().TASK_STATES['AWAITING_REVIEW'])
            )
            logger.info("数据库升级：添加error_category字段并迁移历史翻译失败任务")
            conn.commit()

        if 'upload_target' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_target TEXT DEFAULT 'acfun'")
            logger.info("数据库升级：添加upload_target字段")
            conn.commit()

        if 'bilibili_upload_response' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN bilibili_upload_response TEXT")
            logger.info("数据库升级：添加bilibili_upload_response字段")
            conn.commit()

        if 'recommended_partition_id_acfun' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN recommended_partition_id_acfun TEXT")
            logger.info("数据库升级：添加recommended_partition_id_acfun字段")
            conn.commit()

        if 'selected_partition_id_acfun' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN selected_partition_id_acfun TEXT")
            logger.info("数据库升级：添加selected_partition_id_acfun字段")
            conn.commit()

        if 'recommended_partition_id_bilibili' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN recommended_partition_id_bilibili TEXT")
            logger.info("数据库升级：添加recommended_partition_id_bilibili字段")
            conn.commit()

        if 'selected_partition_id_bilibili' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN selected_partition_id_bilibili TEXT")
            logger.info("数据库升级：添加selected_partition_id_bilibili字段")
            conn.commit()

        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]

        # 历史任务分区字段回填迁移：仅执行一次，避免重复全表扫描
        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key = ? LIMIT 1",
            ('tasks_partition_backfill_v1',)
        )
        partition_backfill_done = cursor.fetchone() is not None

        if not partition_backfill_done:
            try:
                if 'selected_partition_id' in columns and 'selected_partition_id_acfun' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET selected_partition_id_acfun = selected_partition_id
                        WHERE (upload_target = 'acfun' OR upload_target IS NULL OR TRIM(upload_target) = '')
                          AND (selected_partition_id_acfun IS NULL OR TRIM(selected_partition_id_acfun) = '')
                          AND selected_partition_id IS NOT NULL
                          AND TRIM(selected_partition_id) <> ''
                        """
                    )

                if 'recommended_partition_id' in columns and 'recommended_partition_id_acfun' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET recommended_partition_id_acfun = recommended_partition_id
                        WHERE (upload_target = 'acfun' OR upload_target IS NULL OR TRIM(upload_target) = '')
                          AND (recommended_partition_id_acfun IS NULL OR TRIM(recommended_partition_id_acfun) = '')
                          AND recommended_partition_id IS NOT NULL
                          AND TRIM(recommended_partition_id) <> ''
                        """
                    )

                if 'selected_partition_id' in columns and 'selected_partition_id_bilibili' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET selected_partition_id_bilibili = selected_partition_id
                        WHERE upload_target = 'bilibili'
                          AND (selected_partition_id_bilibili IS NULL OR TRIM(selected_partition_id_bilibili) = '')
                          AND selected_partition_id IS NOT NULL
                          AND TRIM(selected_partition_id) <> ''
                        """
                    )

                if 'selected_partition_id' in columns:
                    if 'selected_partition_id_acfun' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET selected_partition_id_acfun = selected_partition_id
                            WHERE upload_target = 'both'
                              AND (selected_partition_id_acfun IS NULL OR TRIM(selected_partition_id_acfun) = '')
                              AND selected_partition_id IS NOT NULL
                              AND TRIM(selected_partition_id) <> ''
                            """
                        )
                    if 'selected_partition_id_bilibili' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET selected_partition_id_bilibili = selected_partition_id
                            WHERE upload_target = 'both'
                              AND (selected_partition_id_bilibili IS NULL OR TRIM(selected_partition_id_bilibili) = '')
                              AND selected_partition_id IS NOT NULL
                              AND TRIM(selected_partition_id) <> ''
                            """
                        )

                if 'recommended_partition_id' in columns and 'recommended_partition_id_bilibili' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET recommended_partition_id_bilibili = recommended_partition_id
                        WHERE upload_target = 'bilibili'
                          AND (recommended_partition_id_bilibili IS NULL OR TRIM(recommended_partition_id_bilibili) = '')
                          AND recommended_partition_id IS NOT NULL
                          AND TRIM(recommended_partition_id) <> ''
                        """
                    )

                if 'recommended_partition_id' in columns:
                    if 'recommended_partition_id_acfun' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET recommended_partition_id_acfun = recommended_partition_id
                            WHERE upload_target = 'both'
                              AND (recommended_partition_id_acfun IS NULL OR TRIM(recommended_partition_id_acfun) = '')
                              AND recommended_partition_id IS NOT NULL
                              AND TRIM(recommended_partition_id) <> ''
                            """
                        )
                    if 'recommended_partition_id_bilibili' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET recommended_partition_id_bilibili = recommended_partition_id
                            WHERE upload_target = 'both'
                              AND (recommended_partition_id_bilibili IS NULL OR TRIM(recommended_partition_id_bilibili) = '')
                              AND recommended_partition_id IS NOT NULL
                              AND TRIM(recommended_partition_id) <> ''
                            """
                        )

                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (migration_key) VALUES (?)",
                    ('tasks_partition_backfill_v1',)
                )
                conn.commit()
                logger.info("数据库升级：历史任务分区字段回填迁移完成")
            except Exception as e2:
                conn.rollback()
                logger.warning("数据库升级：历史任务分区字段回填迁移失败，将在下次启动重试: %s", e2)
        else:
            logger.info("数据库升级：历史任务分区字段回填迁移已执行，跳过")

        if 'asr_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN asr_warning_message TEXT")
            logger.info("数据库升级：添加asr_warning_message字段")
            conn.commit()

        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subtitle_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_warning_message TEXT")
            logger.info("数据库升级：添加subtitle_warning_message字段")
            conn.commit()

        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subtitle_quality_state' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_quality_state TEXT")
            logger.info("数据库升级：添加subtitle_quality_state字段")
            conn.commit()

        # 数据迁移：将 error_message 中纯 ASR/VAD 警告 token 挪至 asr_warning_message，清空 error_message
        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key = ? LIMIT 1",
            ('asr_warning_message_migrate_v1',)
        )
        if cursor.fetchone() is None:
            try:
                _asr_warning_tokens = (
                    'vad_low_coverage', 'vad_no_speech', 'vad_failed',
                    'asr_no_timestamps', 'asr_failed', 'vad_no_usable_window',
                )
                for _token in _asr_warning_tokens:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET asr_warning_message = TRIM(error_message),
                            error_message = NULL
                        WHERE (asr_warning_message IS NULL OR TRIM(asr_warning_message) = '')
                          AND TRIM(error_message) = ?
                        """,
                        (_token,)
                    )
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (migration_key) VALUES (?)",
                    ('asr_warning_message_migrate_v1',)
                )
                conn.commit()
                logger.info("数据库升级：ASR警告字段数据迁移完成")
            except Exception as _em:
                conn.rollback()
                logger.warning("数据库升级：ASR警告字段数据迁移失败: %s", _em)

        # 兼容历史任务：空 upload_target 默认回填 acfun
        cursor.execute("UPDATE tasks SET upload_target = 'acfun' WHERE upload_target IS NULL OR TRIM(upload_target) = ''")
        conn.commit()
    except Exception as e:
        logger.warning(f"数据库升级检查失败（可能已是最新版本）: {e}")
    
    conn.close()
    
    logger.info("数据库初始化完成")

def get_db_path():
    """获取数据库文件路径"""
    return DB_PATH

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_CONNECT_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row  # 返回字典形式的结果
    try:
        conn.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
    except Exception as e:
        logger.debug(f"设置SQLite连接参数失败，将使用默认参数: {e}")
    return conn

def add_task(youtube_url, upload_target=None):
    """
    添加新任务到数据库
    
    Args:
        youtube_url: YouTube视频URL
        upload_target: 投稿平台(acfun|bilibili|both)，为空则使用配置默认值
        
    Returns:
        task_id: 新创建的任务ID
    """
    task_id = str(uuid.uuid4())
    normalized_target = _public().normalize_upload_target(upload_target)
    conn = _public().get_db_connection()
    
    try:
        if not upload_target:
            try:
                from modules.config_manager import load_config
                cfg = load_config()
                normalized_target = _public().normalize_upload_target(cfg.get('UPLOAD_TARGET_DEFAULT', _public().UPLOAD_TARGET_ACFUN))
            except Exception:
                normalized_target = _public().UPLOAD_TARGET_ACFUN
        conn.execute(
            'INSERT INTO tasks (id, youtube_url, upload_target, status) VALUES (?, ?, ?, ?)',
            (task_id, youtube_url, normalized_target, _public().TASK_STATES['PENDING'])
        )
        conn.commit()
        logger.info(f"新任务添加成功, ID: {task_id}, URL: {youtube_url}, 平台: {normalized_target}")
        
        # 新任务添加后，触发全局任务处理器检查是否需要启动任务
        try:
            from modules.config_manager import load_config
            config = load_config()
            processor = _public().get_global_task_processor(config)
            if processor:
                # 延迟触发，确保数据库事务已提交
                import threading
                import time
                
                def delayed_trigger():
                    time.sleep(0.5)  # 等待0.5秒确保事务提交
                    processor._check_and_start_next_pending_task()
                
                threading.Thread(target=delayed_trigger, daemon=True).start()
                logger.info(f"已触发检查pending任务: {task_id}")
        except Exception as e:
            logger.warning(f"触发任务检查失败，但任务已成功添加: {str(e)}")
            
    except Exception as e:
        logger.error(f"添加任务失败: {str(e)}")
        task_id = None
    finally:
        conn.close()
    if task_id:
        publish_task_event('task_added', {'task_id': task_id})
        emit_notification_event(
            NotificationEvent(
                event_type=EVENT_TASK_ADDED,
                payload={
                    'task_id': task_id,
                    'youtube_url': youtube_url,
                    'upload_target': normalized_target,
                },
            )
        )

    return task_id

def update_task(task_id, silent=False, **kwargs):
    """
    更新任务信息
    
    Args:
        task_id: 任务ID
        silent: 是否静默更新（不记录到主日志）
        **kwargs: 要更新的字段及其值
    
    Returns:
        success: 更新是否成功
    """
    if not kwargs:
        return False
    
    # 记录状态变化
    status_changed = 'status' in kwargs
    new_status = kwargs.get('status') if status_changed else None

    # 放弃操作由调用方通过 FAILED + 用户主动放弃任务表示。先设置取消事件，
    # 这样活动 worker 在最终状态写入前仍可观察到取消请求。
    abandon_requested = (
        status_changed
        and new_status == _public().TASK_STATES['FAILED']
        and '主动放弃' in str(kwargs.get('error_message') or '')
    )
    if abandon_requested:
        request_task_cancel(task_id)

    # 添加更新时间
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 白名单验证：只允许更新有效的列名
    ALLOWED_COLUMNS = {
        'youtube_url': 'youtube_url = ?',
        'upload_target': 'upload_target = ?',
        'status': 'status = ?',
        'created_at': 'created_at = ?',
        'updated_at': 'updated_at = ?',
        'video_title_original': 'video_title_original = ?',
        'video_title_translated': 'video_title_translated = ?',
        'description_original': 'description_original = ?',
        'description_translated': 'description_translated = ?',
        'tags_generated': 'tags_generated = ?',
        'recommended_partition_id': 'recommended_partition_id = ?',
        'selected_partition_id': 'selected_partition_id = ?',
        'recommended_partition_id_acfun': 'recommended_partition_id_acfun = ?',
        'selected_partition_id_acfun': 'selected_partition_id_acfun = ?',
        'recommended_partition_id_bilibili': 'recommended_partition_id_bilibili = ?',
        'selected_partition_id_bilibili': 'selected_partition_id_bilibili = ?',
        'cover_path_local': 'cover_path_local = ?',
        'video_path_local': 'video_path_local = ?',
        'subtitle_path_original': 'subtitle_path_original = ?',
        'subtitle_path_translated': 'subtitle_path_translated = ?',
        'subtitle_language_detected': 'subtitle_language_detected = ?',
        'subtitle_qc_failed': 'subtitle_qc_failed = ?',
        'subtitle_qc_reason': 'subtitle_qc_reason = ?',
        'subtitle_qc_score': 'subtitle_qc_score = ?',
        'subtitle_qc_checked_at': 'subtitle_qc_checked_at = ?',
        'metadata_json_path_local': 'metadata_json_path_local = ?',
        'moderation_result': 'moderation_result = ?',
        'error_message': 'error_message = ?',
        'error_category': 'error_category = ?',
        'pipeline_checkpoint': 'pipeline_checkpoint = ?',
        'upload_progress': 'upload_progress = ?',
        'acfun_upload_response': 'acfun_upload_response = ?',
        'bilibili_upload_response': 'bilibili_upload_response = ?',
        'asr_warning_message': 'asr_warning_message = ?',
        'subtitle_warning_message': 'subtitle_warning_message = ?',
        'subtitle_quality_state': 'subtitle_quality_state = ?',
    }

    # 过滤掉不在白名单中的列
    filtered_items = [(k, v) for k, v in kwargs.items() if k in ALLOWED_COLUMNS]
    filtered_kwargs = dict(filtered_items)

    if not filtered_kwargs:
        return False

    # 取消请求发出后，活动 worker 可能仍从旧快照继续执行；禁止其把任务写回
    # READY_FOR_UPLOAD/COMPLETED 等状态。FAILED 是取消协议的统一终态，仍允许写入。
    # 状态写入期间持有取消锁，保证“检查事件 -> SQL 提交”不可被 abandon 插入。
    cancel_guard_acquired = False
    if status_changed:
        _TASK_CANCEL_LOCK.acquire()
        cancel_guard_acquired = True
    if status_changed and new_status != _public().TASK_STATES['FAILED']:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        if event and event.is_set():
            _TASK_CANCEL_LOCK.release()
            cancel_guard_acquired = False
            logger.info(f"任务 {task_id} 已请求取消，忽略状态回写: {new_status}")
            return False

    # 构建SQL更新语句
    set_clause = ', '.join(ALLOWED_COLUMNS[key] for key, _ in filtered_items)
    values = [value for _, value in filtered_items]
    values.append(task_id)
    
    conn = _public().get_db_connection()
    try:
        for attempt in range(1, DB_WRITE_RETRY_TIMES + 1):
            try:
                existing_row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
                existing_task = dict(existing_row) if existing_row else None
                previous_status = existing_task.get('status') if existing_task else None
                conn.execute(
                    f'UPDATE tasks SET {set_clause} WHERE id = ?',
                    values
                )
                conn.commit()

                event_type = 'task_updated'
                event_payload = {
                    'task_id': task_id,
                    'status': filtered_kwargs.get('status')
                }

                changed_fields = {key for key in filtered_kwargs.keys() if key != 'updated_at'}
                if changed_fields == {'upload_progress'}:
                    current_status = filtered_kwargs.get('status')
                    if current_status is None:
                        try:
                            row = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
                            current_status = row['status'] if row else None
                        except Exception:
                            current_status = None

                    event_type = 'task_progress'
                    event_payload = {
                        'task_id': task_id,
                        'status': current_status,
                        'upload_progress': filtered_kwargs.get('upload_progress'),
                        'updated_at': filtered_kwargs.get('updated_at')
                    }

                publish_task_event(event_type, event_payload)
                if (
                    status_changed
                    and previous_status != filtered_kwargs.get('status')
                    and filtered_kwargs.get('status') in (_public().TASK_STATES['COMPLETED'], _public().TASK_STATES['FAILED'])
                ):
                    merged_task = dict(existing_task or {})
                    merged_task.update(filtered_kwargs)
                    merged_task['id'] = task_id
                    emit_notification_event(
                        NotificationEvent(
                            event_type=(
                                EVENT_TASK_COMPLETED
                                if filtered_kwargs.get('status') == _public().TASK_STATES['COMPLETED']
                                else EVENT_TASK_FAILED
                            ),
                            payload=_public()._build_task_notification_payload(
                                merged_task,
                                overrides={'previous_status': previous_status},
                            ),
                        )
                    )
                if not silent:  # 只有非静默模式才记录到主日志
                    logger.info(f"任务 {task_id} 更新成功: {filtered_kwargs}")
                return True
            except sqlite3.OperationalError as e:
                err_msg = str(e).lower()
                is_locked = (
                    'database is locked' in err_msg
                    or 'database is busy' in err_msg
                    or 'locked' in err_msg
                    or 'busy' in err_msg
                )
                if is_locked and attempt < DB_WRITE_RETRY_TIMES:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    time.sleep(DB_WRITE_RETRY_SLEEP_SECONDS * attempt)
                    continue
                logger.error(
                    f"更新任务 {task_id} 失败 (attempt={attempt}/{DB_WRITE_RETRY_TIMES}): {str(e)}"
                )
                return False
            except Exception as e:
                logger.error(f"更新任务 {task_id} 失败: {str(e)}")
                return False
    finally:
        conn.close()
        if cancel_guard_acquired:
            _TASK_CANCEL_LOCK.release()

def get_task(task_id):
    """
    获取任务信息
    
    Args:
        task_id: 任务ID
    
    Returns:
        task: 任务信息字典，如果不存在则返回None
    """
    logger.debug(f"正在获取任务 {task_id}")
    conn = _public().get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        task = cursor.fetchone()
        result = dict(task) if task else None
        logger.debug(f"获取任务 {task_id} 结果: {result}")
        return result
    except Exception as e:
        logger.error(f"获取任务 {task_id} 失败: {str(e)}")
        return None
    finally:
        conn.close()

def get_all_tasks():
    """
    获取所有任务信息
    
    Returns:
        tasks: 任务信息列表
    """
    conn = _public().get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks ORDER BY created_at DESC')
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"获取所有任务失败: {str(e)}")
        return []
    finally:
        conn.close()

def get_tasks_paginated(page=1, per_page=20):
    """
    获取分页任务信息
    
    Args:
        page (int): 页码，从1开始
        per_page (int): 每页数量，默认20
    
    Returns:
        dict: 包含tasks、total、page、per_page、total_pages等信息的字典
    """
    conn = _public().get_db_connection()
    try:
        # 获取总数
        cursor = conn.execute('SELECT COUNT(*) FROM tasks')
        total = cursor.fetchone()[0]
        
        # 计算分页参数
        total_pages = (total + per_page - 1) // per_page  # 向上取整
        offset = (page - 1) * per_page
        
        # 获取分页数据
        cursor = conn.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?', 
                            (per_page, offset))
        tasks = [dict(row) for row in cursor.fetchall()]
        
        return {
            'tasks': tasks,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_page': page - 1 if page > 1 else None,
            'next_page': page + 1 if page < total_pages else None
        }
    except Exception as e:
        logger.error(f"获取分页任务失败: {str(e)}")
        return {
            'tasks': [],
            'total': 0,
            'page': page,
            'per_page': per_page,
            'total_pages': 0,
            'has_prev': False,
            'has_next': False,
            'prev_page': None,
            'next_page': None
        }
    finally:
        conn.close()

def get_tasks_by_status(status):
    """
    获取指定状态的任务
    
    Args:
        status: 任务状态
    
    Returns:
        tasks: 任务信息列表
    """
    conn = _public().get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC', (status,))
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"获取{status}状态任务失败: {str(e)}")
        return []
    finally:
        conn.close()

def delete_task(task_id, delete_files=True):
    """
    删除任务
    
    Args:
        task_id: 任务ID
        delete_files: 是否同时删除任务文件
    
    Returns:
        success: 删除是否成功
    """
    # 先获取任务信息，用于删除文件
    task = get_task(task_id)
    if not task:
        logger.warning(f"任务 {task_id} 不存在，无法删除")
        return False
    
    # 标记任务取消，尽快中断运行中的任务
    request_task_cancel(task_id)

    # 删除任务文件
    if delete_files:
        _public().delete_task_files(task_id)
    
    # 删除任务记录
    conn = _public().get_db_connection()
    try:
        conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        conn.commit()
        logger.info(f"任务 {task_id} 删除成功")
        publish_task_event('task_deleted', {'task_id': task_id})
        return True
    except Exception as e:
        logger.error(f"删除任务 {task_id} 失败: {str(e)}")
        return False
    finally:
        conn.close()

def clear_all_tasks(delete_files=True):
    """
    清空所有任务
    
    Args:
        delete_files: 是否同时删除任务文件
    
    Returns:
        success: 是否成功
    """
    # 标记任务取消，尽快中断运行中的任务
    tasks = get_all_tasks()
    for task in tasks:
        request_task_cancel(task['id'])

    if delete_files:
        for task in tasks:
            _public().delete_task_files(task['id'])
    
    # 清空任务表
    conn = _public().get_db_connection()
    try:
        conn.execute('DELETE FROM tasks')
        conn.commit()
        logger.info("所有任务已清空")
        publish_task_event('tasks_cleared', {})
        return True
    except Exception as e:
        logger.error(f"清空任务失败: {str(e)}")
        return False
    finally:
        conn.close()
