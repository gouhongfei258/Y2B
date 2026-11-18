"""Cookie sync API.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('cookies_api', 'app.blueprints.cookies_api')

@bp.route('/api/cookies/sync', methods=['POST'])
@login_required
def sync_cookies():
    """
    接收从浏览器扩展同步过来的Cookie
    """
    try:
        if not request.is_json:
            return jsonify({'error': '请求必须是JSON格式'}), 400
        
        data = request.get_json()
        
        # 验证必要字段
        required_fields = ['source', 'timestamp', 'cookies', 'cookieCount']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'缺少必要字段: {field}'}), 400
        
        # 验证来源
        if data['source'] not in ['userscript', 'extension']:
            return jsonify({'error': '不支持的cookie来源'}), 400
        
        # 验证cookie数据
        cookies_content = data['cookies']
        if not cookies_content or not isinstance(cookies_content, str):
            return jsonify({'error': 'cookie数据无效'}), 400
        
        # 保存cookie到文件
        cookies_dir = get_app_subdir('cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        # 写入新的cookie文件
        try:
            with open(youtube_cookies_path, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            
            # 记录同步信息
            sync_info = {
                'timestamp': data['timestamp'],
                'sync_time': time.time(),
                'cookie_count': data['cookieCount'],
                'user_agent': data.get('userAgent', ''),
                'source_url': data.get('url', ''),
                'file_size': len(cookies_content)
            }
            
            source_name = '浏览器扩展' if data['source'] == 'extension' else '油猴脚本'
            logger.info(f"Cookie同步成功 - 来源: {source_name}, 数量: {data['cookieCount']}, 大小: {len(cookies_content)} bytes")

            return jsonify({
                'success': True,
                'message': 'Cookie同步成功',
                'sync_info': sync_info
            }), 200
            
        except Exception as e:
            logger.error(f"写入cookie文件失败: {str(e)}")
            return jsonify({'error': '保存cookie失败，请稍后重试'}), 500

    except Exception as e:
        logger.error(f"Cookie同步API异常: {str(e)}")
        return jsonify({'error': '服务器内部错误，请稍后重试'}), 500

@bp.route('/api/cookies/status', methods=['GET'])
@login_required
def get_cookie_status():
    """
    提供Cookie状态给浏览器扩展
    """
    try:
        cookies_dir = get_app_subdir('cookies')
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        status = {
            'youtube_cookies_exists': os.path.exists(youtube_cookies_path),
            'last_modified': None,
            'file_size': 0,
            'line_count': 0
        }
        
        if status['youtube_cookies_exists']:
            stat_info = os.stat(youtube_cookies_path)
            status['last_modified'] = stat_info.st_mtime
            status['file_size'] = stat_info.st_size
            
            # 统计行数
            try:
                with open(youtube_cookies_path, 'r', encoding='utf-8') as f:
                    status['line_count'] = sum(1 for line in f if line.strip() and not line.startswith('#'))
            except Exception as e:
                logger.warning(f"读取cookie文件失败: {str(e)}")
                status['line_count'] = -1
        
        return jsonify(status), 200
        
    except Exception as e:
        logger.error(f"获取cookie状态失败: {str(e)}")
        return jsonify({'error': '获取状态失败，请稍后重试'}), 500

@bp.route('/api/cookies/refresh-needed', methods=['POST'])
@login_required
def cookie_refresh_needed():
    """
    接收浏览器扩展的通知，标记某个网站的Cookie需要刷新
    """
    try:
        data = request.get_json()
        reason = data.get('reason', 'unknown')
        video_url = data.get('video_url', '')
        
        logger.warning(f"收到Cookie刷新需求 - 原因: {reason}, 视频: {video_url}")
        
        # 这里可以实现通知机制，比如：
        # 1. 发送到浏览器扩展
        # 2. 在Web界面显示提示
        # 3. 发送邮件通知等
        
        return jsonify({
            'success': True,
            'message': 'Cookie刷新需求已记录',
            'suggestion': '请使用浏览器扩展重新同步Cookie'
        }), 200
        
    except Exception as e:
        logger.error(f"处理Cookie刷新需求失败: {str(e)}")
        return jsonify({'error': '处理失败，请稍后重试'}), 500

app.register_blueprint(bp)
