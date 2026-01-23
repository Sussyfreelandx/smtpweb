import time
import logging
import traceback
from datetime import datetime, timedelta
from app import db, celery

logger = logging.getLogger(__name__)


def log_task(message, level="INFO"):
    """Log task activity with timestamp."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] TASK-{level}: {message}")
    if level == "ERROR":
        logger.error(message)
    else:
        logger.info(message)


@celery.task(bind=True, name='app.tasks.send_campaign_task', max_retries=5, default_retry_delay=60)
def send_campaign_task(self, campaign_id):
    """
    Main Celery task to send a campaign.
    Uses SMTPRotationManager to rotate SMTP profiles per recipient.
    Uses SMTPHandler connection pooling to speed-up sending.
    Implements exponential backoff behavior for rate-limited failures and improved handler selection heuristics.
    """
    from app import create_app
    from app.models import Campaign, Recipient, SMTPServer
    from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager
    from app.core_logic.personalization import PersonalizationEngine

    log_task("=" * 60)
    log_task(f"📧 TASK RECEIVED: send_campaign_task")
    log_task(f"   Campaign ID: {campaign_id}")
    log_task(f"   Task ID: {getattr(self.request, 'id', 'unknown')}")
    log_task("=" * 60)

    app = create_app()

    with app.app_context():
        # Import broadcast helpers here to avoid circular import at module import time
        try:
            from app.events import broadcast_recipient_update, broadcast_campaign_progress, send_notification
        except Exception:
            # If events cannot be imported, define no-op fallbacks
            def broadcast_recipient_update(campaign_id, recipient_id, status, data=None): pass
            def broadcast_campaign_progress(campaign_id, sent, failed, total, current_email=None): pass
            def send_notification(user_id, title, message, notification_type='info', related_type=None, related_id=None): pass

        try:
            # Load campaign
            campaign = Campaign.query.get(campaign_id)

            if not campaign:
                log_task(f"Campaign {campaign_id} not found", "ERROR")
                return {"status": "error", "message": "Campaign not found"}

            log_task(f"Campaign:  '{campaign.name}', Status: {campaign.status}")

            if campaign.status != 'Sending':
                log_task(f"Campaign not in 'Sending' status.  Current:  {campaign.status}", "WARNING")
                return {"status": "skipped", "message": f"Status is {campaign.status}"}

            # Build list of SMTP profiles for rotation (only active ones owned by user/team)
            profiles_query = SMTPServer.query.filter(
                SMTPServer.user_id == campaign.user_id
            )
            profiles = []
            for p in profiles_query.all():
                if p.is_active:
                    # Build dict that includes runtime counters needed by rotation manager
                    profiles.append({
                        'id': p.id,
                        'profile_name': p.profile_name,
                        'server': p.server,
                        'port': p.port,
                        'use_tls': p.use_tls,
                        'use_ssl': p.use_ssl,
                        'username': p.username,
                        'password': p.get_password(),
                        'sender_name': p.sender_name,
                        'sender_email': p.sender_email,
                        'daily_limit': p.daily_limit,
                        'sent_today': p.sent_today,
                        'priority': p.priority or 1,
                        'reputation_score': p.reputation_score if hasattr(p, 'reputation_score') else 100.0
                    })

            if not profiles:
                # fallback: use campaign assigned SMTP profile if any
                smtp_profile = campaign.smtp_profile
                if smtp_profile:
                    profiles = [{
                        'id': smtp_profile.id,
                        'profile_name': smtp_profile.profile_name,
                        'server': smtp_profile.server,
                        'port': smtp_profile.port,
                        'use_tls': smtp_profile.use_tls,
                        'use_ssl': smtp_profile.use_ssl,
                        'username': smtp_profile.username,
                        'password': smtp_profile.get_password(),
                        'sender_name': smtp_profile.sender_name,
                        'sender_email': smtp_profile.sender_email,
                        'daily_limit': smtp_profile.daily_limit,
                        'sent_today': smtp_profile.sent_today,
                        'priority': smtp_profile.priority or 1,
                        'reputation_score': smtp_profile.reputation_score if hasattr(smtp_profile, 'reputation_score') else 100.0
                    }]
                else:
                    log_task("No SMTP profiles available for rotation", "ERROR")
                    campaign.status = 'Failed'
                    db.session.commit()
                    return {"status": "error", "message": "No SMTP profiles"}

            # Create rotation manager
            rotation = SMTPRotationManager(profiles, handler_pool_size=campaign.parallel_workers or 3)

            # Sending config
            batch_size = campaign.throttle_amount or 20
            delay_seconds = campaign.throttle_delay or 60
            parallel_workers = min(campaign.parallel_workers or 10, 20)

            log_task(f"Config: batch={batch_size}, delay={delay_seconds}s, parallel_workers={parallel_workers}")

            # Get attachments
            attachments = []
            if hasattr(campaign, 'get_attachments'):
                try:
                    attachments = campaign.get_attachments() or []
                except:
                    pass

            # For safe counters
            total_sent = campaign.sent_count or 0
            total_failed = campaign.failed_count or 0
            batch_num = 0

            # Main sending loop
            while True:
                batch_num += 1

                # Refresh campaign
                db.session.expire_all()
                campaign = Campaign.query.get(campaign_id)

                if not campaign:
                    log_task("Campaign deleted during send", "ERROR")
                    break

                if campaign.status != 'Sending':
                    log_task(f"Status changed to '{campaign.status}'.  Stopping.", "WARNING")
                    break

                # Get queued recipients (skip ones scheduled for retry via next_retry_at)
                now = datetime.utcnow()
                recipients = campaign.recipients.filter(
                    Recipient.status == 'Queued',
                    (Recipient.next_retry_at == None) | (Recipient.next_retry_at <= now)
                ).limit(batch_size).all()

                if not recipients:
                    log_task("No more queued recipients.  Completing campaign.")
                    campaign.status = 'Completed'
                    campaign.completed_at = datetime.utcnow()
                    campaign.sent_count = total_sent
                    campaign.failed_count = total_failed
                    db.session.commit()
                    break

                log_task(f"--- Batch #{batch_num}:  {len(recipients)} recipients ---")

                # Prepare tasks: assign handler objects intelligently using rotation manager
                tasks = []
                recipient_map = {}
                for recipient in recipients:
                    try:
                        # Mark as sending and increment attempts
                        recipient.status = 'Sending'
                        recipient.attempts = (recipient.attempts or 0) + 1
                        recipient.last_attempt_at = datetime.utcnow()
                        db.session.commit()

                        # Pick handler for this recipient using improved selection
                        handler, handler_key = rotation.get_next_handler()
                        if not handler:
                            # No handler available: mark failed
                            recipient.status = 'Failed'
                            recipient.status_message = f"No SMTP handler available: {handler_key}"
                            db.session.commit()
                            total_failed += 1
                            broadcast_recipient_update(campaign_id, recipient.id, recipient.status, {'message': recipient.status_message})
                            continue

                        # Personalize
                        try:
                            personalizer = PersonalizationEngine(campaign, recipient)
                            subject, body_html, body_plain = personalizer.personalize()
                        except Exception as pe:
                            log_task(f"Personalization error for {recipient.email}: {pe}", "WARNING")
                            subject = campaign.subject
                            body_html = campaign.body_html
                            body_plain = campaign.body_plain or ""

                        task = {
                            'to_email': recipient.email,
                            'subject': subject,
                            'html_content': body_html,
                            'plain_content': body_plain,
                            'attachments': attachments,
                            'custom_headers': None,
                            'recipient_id': recipient.id,
                            'handler': handler,
                            'handler_key': handler_key
                        }
                        tasks.append(task)
                        recipient_map[recipient.email] = recipient
                    except Exception as e:
                        log_task(f"Exception preparing recipient {getattr(recipient, 'email', 'unknown')}: {e}", "ERROR")
                        try:
                            recipient.status = 'Failed'
                            recipient.status_message = str(e)[:250]
                            db.session.commit()
                        except:
                            db.session.rollback()
                        total_failed += 1
                        broadcast_recipient_update(campaign_id, recipient.id, 'Failed', {'message': str(e)[:250]})

                # Run threaded sending across tasks — handlers are responsible for pooling
                results = []
                def _send_task(task):
                    handler = task['handler']
                    try:
                        success, msg = handler.send_email_sync(
                            to_email=task['to_email'],
                            subject=task['subject'],
                            html_content=task['html_content'],
                            plain_content=task.get('plain_content'),
                            attachments=task.get('attachments'),
                            custom_headers=task.get('custom_headers')
                        )
                        return {
                            'email': task['to_email'],
                            'success': success,
                            'error': None if success else msg,
                            'recipient_id': task.get('recipient_id'),
                            'handler_key': task.get('handler_key')
                        }
                    except Exception as e:
                        logger.error(f"Send task exception: {e}")
                        return {
                            'email': task.get('to_email'),
                            'success': False,
                            'error': str(e),
                            'recipient_id': task.get('recipient_id'),
                            'handler_key': task.get('handler_key')
                        }

                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
                    future_to_task = {executor.submit(_send_task, t): t for t in tasks}
                    for future in as_completed(future_to_task):
                        try:
                            res = future.result()
                            results.append(res)
                        except Exception as e:
                            logger.error(f"Future exception: {e}")
                            res = {'email': 'unknown', 'success': False, 'error': str(e)}
                            results.append(res)

                # Process results with exponential backoff for rate-limited errors
                for res in results:
                    email = res.get('email')
                    success = res.get('success', False)
                    error_msg = (res.get('error') or '') or ''
                    recipient = None
                    try:
                        recipient = Recipient.query.filter_by(campaign_id=campaign_id, email=email).first()
                    except Exception:
                        recipient = recipient_map.get(email)

                    if not recipient:
                        continue

                    try:
                        err_lower = error_msg.lower() if error_msg else ''
                        if success:
                            recipient.status = 'Sent'
                            recipient.sent_at = datetime.utcnow()
                            recipient.status_message = 'OK'
                            total_sent += 1
                            broadcast_recipient_update(campaign_id, recipient.id, 'Sent', {'email': email})
                        else:
                            # Handle rate limiting with exponential backoff
                            if 'rate' in err_lower or 'throttl' in err_lower or 'limit' in err_lower:
                                # exponential backoff based on attempts
                                attempts = recipient.attempts or 1
                                backoff_seconds = min(3600, (2 ** (attempts - 1)) * 30)  # 30s,1m,2m,4m...
                                recipient.next_retry_at = datetime.utcnow() + timedelta(seconds=backoff_seconds)
                                recipient.status = 'Queued'  # requeue for retry later
                                recipient.status_message = f"Rate-limited, retrying in {backoff_seconds}s"
                                db.session.commit()
                                broadcast_recipient_update(campaign_id, recipient.id, 'Queued', {'message': recipient.status_message})
                                continue
                            # Other errors mark as failed
                            recipient.status = 'Failed'
                            recipient.status_message = str(error_msg)[:250] if error_msg else 'Unknown'
                            total_failed += 1
                            # If error indicates auth/connection failure for a handler, mark it failed in rotation
                            handler_key = res.get('handler_key')
                            if handler_key and ('auth' in err_lower or 'connection' in err_lower or 'proxy' in err_lower):
                                try:
                                    rotation._mark_failed(handler_key)
                                except Exception:
                                    pass
                            broadcast_recipient_update(campaign_id, recipient.id, 'Failed', {'email': email, 'error': recipient.status_message})
                        recipient.last_attempt_at = datetime.utcnow()
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        log_task(f"Failed to update recipient {email}: {e}", "ERROR")

                # Update campaign counters in DB safely
                try:
                    campaign.sent_count = campaign.recipients.filter_by(status='Sent').count()
                    campaign.failed_count = campaign.recipients.filter_by(status='Failed').count()
                    db.session.commit()
                except Exception:
                    db.session.rollback()

                # Broadcast batch progress
                try:
                    remaining = campaign.recipients.filter_by(status='Queued').count()
                    total = campaign.recipients.count() + remaining
                    broadcast_campaign_progress(campaign_id, campaign.sent_count or total_sent, campaign.failed_count or total_failed, total, current_email=None)
                except Exception:
                    pass

                log_task(f"Batch #{batch_num} done. Sent: {total_sent}, Failed: {total_failed}")

                # Wait between batches if needed
                remaining = campaign.recipients.filter_by(status='Queued').count()
                if remaining > 0 and delay_seconds > 0:
                    log_task(f"Waiting {delay_seconds}s.  {remaining} remaining.")
                    time.sleep(delay_seconds)

            # Cleanup rotation manager handlers
            try:
                rotation.close_all()
            except Exception:
                pass

            log_task("=" * 60)
            log_task(f"🏁 CAMPAIGN COMPLETE: {campaign.name if campaign else campaign_id}")
            log_task(f"   Sent: {campaign.sent_count if campaign else total_sent}, Failed: {campaign.failed_count if campaign else total_failed}")
            log_task("=" * 60)

            return {"status": "completed", "sent": campaign.sent_count if campaign else total_sent, "failed": campaign.failed_count if campaign else total_failed}

        except Exception as e:
            log_task(f"💥 CRITICAL ERROR: {e}", "ERROR")
            log_task(traceback.format_exc(), "ERROR")

            try:
                db.session.rollback()
                campaign = Campaign.query.get(campaign_id)
                if campaign:
                    campaign.status = 'Failed'
                    db.session.commit()
            except:
                pass

            # Exponential retry for whole task failures (backoff)
            try:
                countdown = min(3600, (2 ** self.request.retries) * 60)
            except Exception:
                countdown = 60
            raise self.retry(exc=e, countdown=countdown)
