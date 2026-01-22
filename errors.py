from flask import render_template, current_app
from app.main import bp
from app import db

@bp.app_errorhandler(400)
def bad_request(error):
    return render_template('400.html'), 400

@bp.app_errorhandler(403)
def forbidden(error):
    return render_template('403.html'), 403

@bp.app_errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@bp.app_errorhandler(429)
def too_many_requests(error):
    return render_template('429.html'), 429

@bp.app_errorhandler(500)
def internal_error(error):
    db.session.rollback()
    current_app.logger.exception("Unhandled server error: %s", error)
    return render_template('500.html'), 500
