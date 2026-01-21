from flask import Blueprint, current_app, make_response, redirect, request, flash, url_for, render_template
from app.models import Recipient, Suppression
from app import db
import base64
from itsdangerous import URLSafeTimedSerializer as Serializer
from datetime import datetime

bp = Blueprint('tracking', __name__)

@bp.route('/t/o/<token>')
def track_open(token):
    try:
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token, salt='track')
        except:
            return "Invalid token", 400

        recipient_id = data.get('rid')
        recipient = Recipient.query.get(recipient_id)
        
        if recipient:
            if not recipient.opened_at:
                recipient.opened_at = datetime.utcnow()
                recipient.open_count = (recipient.open_count or 0) + 1
                if recipient.status not in ['Clicked', 'Unsubscribed', 'Bounced']: 
                    recipient.status = 'Opened'
                db.session.commit()
    except Exception as e: 
        current_app.logger.error(f"Error tracking open: {e}")

    # Return 1x1 transparent pixel
    pixel_data = base64.b64decode(b'R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
    response = make_response(pixel_data)
    response.headers['Content-Type'] = 'image/gif'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@bp.route('/t/c/<token>')
def track_click(token):
    redirect_url = "https://google.com" # Default fallback
    try:
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token, salt='track')
        except:
            return "Invalid token", 400

        recipient_id = data.get('rid')
        if 'url' in data:
            try:
                redirect_url = base64.urlsafe_b64decode(data['url'].encode()).decode()
            except:
                pass

        recipient = Recipient.query.get(recipient_id)
        if recipient: 
            if not recipient.clicked_at:
                recipient.clicked_at = datetime.utcnow()
            recipient.click_count = (recipient.click_count or 0) + 1
            if recipient.status != 'Unsubscribed': 
                recipient.status = 'Clicked'
            db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error tracking click: {e}")

    return redirect(redirect_url)


@bp.route('/unsub/<token>')
def unsubscribe(token):
    try:
        s = Serializer(current_app.config['SECRET_KEY'])
        try:
            data = s.loads(token, salt='track')
        except:
            return "Invalid or expired unsubscribe link", 400

        recipient_id = data.get('rid')
        recipient = Recipient.query.get(recipient_id)
        
        if recipient:
            recipient.status = 'Unsubscribed'
            recipient.unsubscribed_at = datetime.utcnow()
            db.session.commit()

            if not Suppression.query.filter_by(email=recipient.email).first():
                suppression = Suppression(
                    email=recipient.email, 
                    reason='Unsubscribed by user',
                    source='campaign_link'
                )
                db.session.add(suppression)
                db.session.commit()

            return render_template('message.html', 
                message_title='Unsubscribed',
                message_body=f'You have been successfully unsubscribed from this list ({recipient.email}).',
                message_type='success',
                is_unsubscribe_page=True
            )
            
    except Exception as e: 
        current_app.logger.error(f"Error processing unsubscribe: {e}")
        return "An error occurred while processing your request.", 500

    return "Invalid request", 400
