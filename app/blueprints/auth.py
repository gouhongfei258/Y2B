"""Login and logout.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('auth', 'app.blueprints.auth')

@bp.route('/login', methods=['GET', 'POST'])
def login():
    config = load_config()
    # 如果密码保护未启用，或已登录，则重定向到首页
    if not config.get('password_protection_enabled'):
        return redirect(url_for('index'))
    if 'logged_in' in session:
        return redirect(url_for('index'))

    # 读取登录安全状态
    sec = _load_security_state()
    now_ts = time.time()
    # 检查是否处于锁定期
    if sec.get('locked_until', 0) and now_ts < sec['locked_until']:
        remaining = int(sec['locked_until'] - now_ts)
        minutes = remaining // 60
        seconds = remaining % 60
        flash(f'登录已被临时锁定，请 {minutes} 分 {seconds} 秒后重试。', 'danger')
        return render_template('login.html')

    if request.method == 'POST':
        password = request.form.get('password')
        stored_password = config.get('password')

        # 检查是否有设置密码
        if not stored_password:
            flash('系统尚未设置密码，无法登录。请在禁用密码保护的情况下，进入设置页面设置密码。', 'danger')
            return render_template('login.html')

        if password and password == stored_password:
            session['logged_in'] = True
            session.permanent = True  # session持久化
            # 登录成功，重置失败计数与锁定
            sec.update({'failed_attempts': 0, 'locked_until': 0, 'last_attempt': now_ts})
            _save_security_state(sec)
            _emit_login_event(
                EVENT_LOGIN_SUCCESS,
                {
                    'ip_address': _get_request_ip_address(),
                }
            )
            flash('登录成功', 'success')
            return redirect(url_for('index'))
        else:
            # 密码错误，更新失败计数
            max_attempts = int(config.get('LOGIN_MAX_FAILED_ATTEMPTS', 5) or 5)
            lock_minutes = int(config.get('LOGIN_LOCKOUT_MINUTES', 15) or 15)
            failed = int(sec.get('failed_attempts', 0) or 0) + 1
            sec['failed_attempts'] = failed
            sec['last_attempt'] = now_ts
            # 达到阈值则锁定
            if failed >= max_attempts:
                sec['locked_until'] = now_ts + lock_minutes * 60
                _save_security_state(sec)
                _emit_login_event(
                    EVENT_LOGIN_LOCKED,
                    {
                        'ip_address': _get_request_ip_address(),
                        'failed_attempts': failed,
                        'max_attempts': max_attempts,
                        'lock_minutes': lock_minutes,
                    }
                )
                flash(f'密码错误次数过多（{failed}/{max_attempts}），已锁定 {lock_minutes} 分钟。', 'danger')
            else:
                _save_security_state(sec)
                remain = max_attempts - failed
                flash(f'密码错误。还可尝试 {remain} 次后将被锁定。', 'danger')
    
    return render_template('login.html')

@bp.route('/logout', methods=['POST'])
@login_required
def logout():
    session.clear()
    flash('您已成功退出。', 'info')
    return redirect(url_for('login'))

app.register_blueprint(bp)
