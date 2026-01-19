from flask import Blueprint

bp = Blueprint('webhooks', __name__)

from app.webhooks import routes