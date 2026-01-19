from flask import Blueprint, current_app, make_response, redirect, request, flash, url_for
from app.models import Recipient, Suppression
from app import db
import base64
from datetime import datetime

bp = Blueprint('tracking', __name__)


@bp.route('/t/o/<int:campaign_id>/<int:recipient_id>')
def track_open(campaign_id, recipient_id):
    """Records an email open event."""
    try:
        recipient = Recipient.query.get(recipient_id)
        if recipient and recipient.campaign_id == campaign_id: 
            if not recipient.opened_at:
                recipient.opened_at = datetime. utcnow()
                if recipient.status not in ['Clicked', 'Unsubscribed']:
                    recipient.status = 'Opened'
                db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error tracking open for recipient {recipient_id}: {e}")

    pixel_data = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = make_response(pixel_data)
    response.headers['Content-Type'] = 'image/gif'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@bp.route('/t/c/<int:campaign_id>/<int:recipient_id>')
def track_click(campaign_id, recipient_id):
    """Records a click event and redirects the user."""
    redirect_url = "#"
    try:
        redirect_url_encoded = request.args.get('url')
        if redirect_url_encoded:
            redirect_url = base64.urlsafe_b64decode(redirect_url_encoded. encode()).decode()

        recipient = Recipient.query. get(recipient_id)
        if recipient and recipient.campaign_id == campaign_id:
            if not recipient.clicked_at:
                recipient.clicked_at = datetime.utcnow()
                if recipient.status != 'Unsubscribed':
                    recipient.status = 'Clicked'
                db.session.commit()
    except Exception as e: 
        current_app.logger. error(f"Error tracking click for recipient {recipient_id}: {e}")

    return redirect(redirect_url)


@bp.route('/unsub/<int:campaign_id>/<int:recipient_id>')
def unsubscribe(campaign_id, recipient_id):
    """Handles unsubscribe requests."""
    try:
        recipient = Recipient.query.get(recipient_id)
        if recipient and recipient.campaign_id == campaign_id:
            recipient.status = 'Unsubscribed'
            db.session.commit()
            
            if not Suppression.query.filter_by(email=recipient.email).first():
                suppression = Suppression(email=recipient. email, reason='Unsubscribed')
                db.session. add(suppression)
                db.session.commit()
            
            flash("You have been successfully unsubscribed.", "info")
    except Exception as e: 
        current_app.logger. error(f"Error processing unsubscribe for {recipient_id}: {e}")
        flash("An error occurred while processing your request.", "danger")
    
    return redirect(url_for('main.index'))
