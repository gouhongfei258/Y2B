"""Dashboard and system health.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('dashboard', 'app.blueprints.dashboard')

@bp.route('/')
@login_required
def index():
    """首页"""
    logger.info("访问首页")
    # 统计信息用于仪表盘
    try:
        from y2b.pipeline.task_manager import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()

        # 本地时间的今日起止
        now_local = datetime.now()
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        fmt = "%Y-%m-%d %H:%M:%S"
        start_str = today_start.strftime(fmt)
        end_str = tomorrow_start.strftime(fmt)

        # 各类计数
        cur.execute("SELECT COUNT(*) FROM tasks")
        total_tasks = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['AWAITING_REVIEW'],))
        awaiting_review = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['FAILED'],))
        failed_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['PENDING'],))
        pending_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['READY_FOR_UPLOAD'],))
        ready_total = cur.fetchone()[0] or 0

        # 进行中的状态集合
        processing_states = (
            'fetching_info', 'info_fetched',
            TASK_STATES['TRANSLATING'], TASK_STATES['TAGGING'], TASK_STATES['PARTITIONING'],
            TASK_STATES['MODERATING'], TASK_STATES['DOWNLOADING'], TASK_STATES['DOWNLOADED'],
            TASK_STATES['ASR_TRANSCRIBING'], TASK_STATES['TRANSLATING_SUBTITLE'],
            TASK_STATES['ENCODING_VIDEO'], TASK_STATES['UPLOADING']
        )
        placeholders = ",".join(["?"] * len(processing_states))
        cur.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders})", processing_states)
        in_progress = cur.fetchone()[0] or 0

        # 今日完成/失败/新增任务
        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['COMPLETED'], start_str, end_str)
        )
        completed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['FAILED'], start_str, end_str)
        )
        failed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at < ?",
            (start_str, end_str)
        )
        created_today = cur.fetchone()[0] or 0

        # 最近任务（按更新时间倒序）
        cur.execute(
            "SELECT id, video_title_translated, video_title_original, status, updated_at, upload_target, acfun_upload_response, bilibili_upload_response FROM tasks ORDER BY updated_at DESC LIMIT 10"
        )
        rows = cur.fetchall()
        recent_tasks = []
        for r in rows:
            upload_id = None
            upload_target = (r[5] or 'acfun').lower()
            try:
                if upload_target == 'both':
                    resp_b = json.loads(r[7]) if r[7] else None
                    resp_a = json.loads(r[6]) if r[6] else None
                    bv = resp_b.get('bvid') if isinstance(resp_b, dict) else None
                    ac = resp_a.get('ac_number') if isinstance(resp_a, dict) else None
                    if bv and ac:
                        upload_id = f"{bv} / AC{ac}"
                    elif bv:
                        upload_id = bv
                    elif ac:
                        upload_id = f"AC{ac}"
                elif upload_target == 'bilibili':
                    resp = json.loads(r[7]) if r[7] else None
                    if isinstance(resp, dict):
                        upload_id = resp.get('bvid') or resp.get('aid')
                else:
                    resp = json.loads(r[6]) if r[6] else None
                    if isinstance(resp, dict):
                        upload_id = resp.get('ac_number')
            except Exception:
                upload_id = None
            recent_tasks.append({
                'id': r[0],
                'title': r[1] or r[2] or '未获取标题',
                'status': r[3],
                'updated_at': r[4],
                'upload_target': upload_target,
                'upload_id': upload_id
            })

        conn.close()

        stats = {
            'total_tasks': total_tasks,
            'awaiting_review': awaiting_review,
            'failed_total': failed_total,
            'pending_total': pending_total,
            'ready_total': ready_total,
            'in_progress': in_progress,
            'completed_today': completed_today,
            'failed_today': failed_today,
            'created_today': created_today
        }
    except Exception as e:
        logger.warning(f"首页统计失败: {e}")
        stats = {
            'total_tasks': 0,
            'awaiting_review': 0,
            'failed_total': 0,
            'pending_total': 0,
            'ready_total': 0,
            'in_progress': 0,
            'completed_today': 0,
            'failed_today': 0,
            'created_today': 0
        }
        recent_tasks = []

    return render_template(
        'index.html',
        stats=stats,
        recent_tasks=recent_tasks,
        runtime_gates=_runtime_gate_status(),
    )

@bp.route('/system_health')
def system_health():
    """系统健康检查 - 增强Docker环境兼容性"""
    from y2b.pipeline.task_manager import get_db_connection, validate_cookies, resolve_cookie_file_path
    import sqlite3
    import os
    import platform
    import sys
    
    # 检测运行环境
    is_docker = os.path.exists('/.dockerenv') or os.environ.get('CONTAINER') == 'docker'
    
    health_status = {
        'environment': {
            'platform': platform.system(),
            'python_version': sys.version.split()[0],
            'is_docker': is_docker,
            'user': os.environ.get('USER', 'unknown'),
            'working_directory': os.getcwd()
        },
        'database': {'status': 'unknown', 'message': ''},
        'youtube_cookies': {'status': 'unknown', 'message': ''},
        'acfun_cookies': {'status': 'unknown', 'message': ''},
        'bilibili_cookies': {'status': 'unknown', 'message': ''},
        'stuck_tasks': {'count': 0, 'tasks': []},
        'recent_errors': [],
        'docker_volumes': {}
    }
    
    # Docker环境特殊检查
    if is_docker:
        health_status['docker_volumes'] = check_docker_volumes()
    
    # 检查数据库
    try:
        logger.info("开始数据库健康检查...")
        conn = get_db_connection()
        
        # 测试基本连接
        cursor = conn.execute('SELECT COUNT(*) FROM tasks')
        task_count = cursor.fetchone()[0]
        
        # 检查数据库文件权限和位置
        db_info = get_database_info()
        
        health_status['database'] = {
            'status': 'ok',
            'message': f'数据库正常，共有 {task_count} 个任务',
            'location': db_info['path'],
            'size_mb': round(db_info['size'] / 1024 / 1024, 2),
            'writable': db_info['writable']
        }
        
        # 检查卡住的任务
        stuck_cursor = conn.execute('''
            SELECT id, status, created_at, updated_at, error_message
            FROM tasks 
            WHERE status IN ('processing', 'downloading', 'uploading', 'fetching_info', 'translating')
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        ''')
        stuck_tasks = stuck_cursor.fetchall()
        health_status['stuck_tasks'] = {
            'count': len(stuck_tasks),
            'tasks': [{'id': t[0][:8] + '...', 'status': t[1], 'updated': t[3]} for t in stuck_tasks]
        }
        
        # 检查最近的错误
        error_cursor = conn.execute('''
            SELECT id, error_message, updated_at
            FROM tasks 
            WHERE status = 'failed' AND error_message IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 5
        ''')
        error_tasks = error_cursor.fetchall()
        health_status['recent_errors'] = [
            {'id': t[0][:8] + '...', 'error': t[1][:100] + '...' if len(t[1]) > 100 else t[1], 'time': t[2]}
            for t in error_tasks
        ]
        
        conn.close()
        logger.info("数据库健康检查完成")
    except Exception:
        logger.exception("数据库健康检查失败")
        health_status['database'] = {
            'status': 'error',
            'message': _public_health_check_error_message('数据库'),
            'details': get_database_debug_info()
        }
    

    # 检查cookies - 使用更健壮的路径处理
    try:
        logger.info("开始cookies健康检查...")
        config = load_config()
        
        # YouTube cookies
        yt_cookies_path = config.get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt')
        if yt_cookies_path:
            # 如果是相对路径，转换为绝对路径
            yt_cookies_path = resolve_cookie_file_path(
                path_value=yt_cookies_path,
                default_relative_path='cookies/yt_cookies.txt',
                service_name='YouTube',
                logger_obj=logger,
                allow_json_txt_fallback=False
            )
            
            try:
                logger.debug(f"检查YouTube cookies文件: {yt_cookies_path}")
                is_valid, message = validate_cookies(yt_cookies_path, "YouTube")
                
                # 获取文件详细信息
                file_info = get_file_info(yt_cookies_path)
                
                health_status['youtube_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': yt_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception:
                logger.exception("YouTube cookies检查异常")
                health_status['youtube_cookies'] = {
                    'status': 'error',
                    'message': _public_health_check_error_message('YouTube Cookies'),
                    'path': yt_cookies_path,
                    'debug_info': get_path_debug_info(yt_cookies_path)
                }
        else:
            health_status['youtube_cookies'] = {
                'status': 'warning',
                'message': '未配置YouTube cookies路径'
            }
        
        # AcFun cookies
        ac_cookies_path = resolve_cookie_file_path(
            path_value=config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
            default_relative_path='cookies/ac_cookies.json',
            service_name='AcFun',
            logger_obj=logger,
            allow_json_txt_fallback=True
        )
        if ac_cookies_path:
            try:
                logger.debug(f"检查AcFun cookies文件: {ac_cookies_path}")
                is_valid, message = validate_cookies(ac_cookies_path, "AcFun")
                
                # 获取文件详细信息
                file_info = get_file_info(ac_cookies_path)
                
                health_status['acfun_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': ac_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception:
                logger.exception("AcFun cookies检查异常")
                health_status['acfun_cookies'] = {
                    'status': 'error',
                    'message': _public_health_check_error_message('AcFun Cookies'),
                    'path': ac_cookies_path,
                    'debug_info': get_path_debug_info(ac_cookies_path)
                }
        else:
            health_status['acfun_cookies'] = {
                'status': 'warning',
                'message': '未配置AcFun cookies路径'
            }

        # Bilibili cookies
        bili_cookies_path = config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json')
        if bili_cookies_path:
            bili_cookies_path = resolve_cookie_file_path(
                path_value=bili_cookies_path,
                default_relative_path='cookies/bili_cookies.json',
                service_name='Bilibili',
                logger_obj=logger,
                allow_json_txt_fallback=False
            )

            try:
                logger.debug(f"检查Bilibili cookies文件: {bili_cookies_path}")
                is_valid, message = validate_cookies(bili_cookies_path, "Bilibili")
                file_info = get_file_info(bili_cookies_path)
                health_status['bilibili_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': bili_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception:
                logger.exception("Bilibili cookies检查异常")
                health_status['bilibili_cookies'] = {
                    'status': 'error',
                    'message': _public_health_check_error_message('Bilibili Cookies'),
                    'path': bili_cookies_path,
                    'debug_info': get_path_debug_info(bili_cookies_path)
                }
        else:
            health_status['bilibili_cookies'] = {
                'status': 'warning',
                'message': '未配置Bilibili cookies路径'
            }
        
        logger.info("cookies健康检查完成")
            
    except Exception:
        logger.exception("检查cookies时发生错误")
        health_status['youtube_cookies'] = {
            'status': 'error',
            'message': _public_health_check_error_message('YouTube Cookies'),
            'debug_info': _public_health_check_error_message('Cookies')
        }
        health_status['acfun_cookies'] = {
            'status': 'error',
            'message': _public_health_check_error_message('AcFun Cookies'),
            'debug_info': _public_health_check_error_message('Cookies')
        }
        health_status['bilibili_cookies'] = {
            'status': 'error',
            'message': _public_health_check_error_message('Bilibili Cookies'),
            'debug_info': _public_health_check_error_message('Cookies')
        }
    
    return jsonify(health_status)

app.register_blueprint(bp)
