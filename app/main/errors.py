from flask import render_template, current_app, jsonify, request
from app.main import bp
from app import db
from jinja2 import TemplateNotFound

def wants_json_response():
    return request.accept_mimetypes['application/json'] >= \
        request.accept_mimetypes['text/html']

@bp.app_errorhandler(400)
def bad_request(error):
    if wants_json_response():
        return jsonify(error="Bad Request", message=str(error)), 400
    try:
        return render_template('400.html'), 400
    except TemplateNotFound:
        return "400 Bad Request", 400

@bp.app_errorhandler(403)
def forbidden(error):
    if wants_json_response():
        return jsonify(error="Forbidden", message=str(error)), 403
    try:
        return render_template('403.html'), 403
    except TemplateNotFound:
        return "403 Forbidden", 403

@bp.app_errorhandler(404)
def not_found_error(error):
    if wants_json_response():
        return jsonify(error="Not Found", message="Resource not found"), 404
    try:
        return render_template('404.html'), 404
    except TemplateNotFound:
        return "404 Not Found", 404

@bp.app_errorhandler(429)
def too_many_requests(error):
    if wants_json_response():
        return jsonify(error="Too Many Requests", message=str(error)), 429
    try:
        return render_template('429.html'), 429
    except TemplateNotFound:
        return "429 Too Many Requests", 429

@bp.app_errorhandler(500)
def internal_error(error):
    db.session.rollback()
    current_app.logger.exception("Internal Server Error: %s", error)
    
    if wants_json_response():
        return jsonify(error="Internal Server Error", message="An unexpected error occurred"), 500
        
    try:
        return render_template('500.html'), 500
    except TemplateNotFound:
        return "500 Internal Server Error (Template '500.html' not found)", 500
