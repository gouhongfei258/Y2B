"""Upload template editor.

Executed by create_app() in the app package namespace. Endpoint names stay
unprefixed so existing url_for() names and request.endpoint checks keep working.
"""
from app.blueprints.blueprint import Blueprint

bp = Blueprint('upload_templates', 'app.blueprints.upload_templates')

@bp.route('/upload-templates', methods=['GET', 'POST'])
@login_required
def upload_templates_page():
    from y2b.bilibili.bilibili_accounts import list_accounts, public_account
    from y2b.config.upload_templates import list_templates, public_template, save_template
    config = load_config()
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        try:
            record = save_template(payload, template_id=str(payload.get('id') or ''))
        except Exception as exc:
            logger.exception("保存投稿模板失败")
            return jsonify({'success': False, 'message': '保存模板失败'}), 500
        return jsonify({
            'success': True,
            'message': f'模板「{record.get("name")}」已保存',
            'template': public_template(record),
            'templates': [public_template(item) for item in list_templates()],
        })
    accounts = [public_account(item) for item in list_accounts(config)]
    return render_template(
        'upload_templates.html',
        templates=[public_template(item) for item in list_templates()],
        accounts=accounts,
        config=config,
    )

@bp.route('/upload-templates/preview', methods=['POST'])
@login_required
def upload_templates_preview():
    from y2b.config.upload_templates import preview_fields
    payload = request.get_json(silent=True) or request.form
    try:
        preview = preview_fields(payload)
    except Exception:
        logger.exception("渲染投稿模板预览失败")
        return jsonify({'success': False, 'message': '预览失败'}), 500
    return jsonify({'success': True, **preview})

@bp.route('/upload-templates/<template_id>/delete', methods=['POST'])
@login_required
def upload_template_delete(template_id):
    from y2b.config.upload_templates import delete_template, list_templates, public_template
    ok, message = delete_template(template_id)
    status = 200 if ok else 400
    return jsonify({
        'success': ok,
        'message': message,
        'templates': [public_template(item) for item in list_templates()],
    }), status

app.register_blueprint(bp)
