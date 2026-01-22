from collections import deque
from datetime import datetime
from flask import current_app
import re
import csv
import io
import json
import os

# ==================== LOGGING ====================

LOG_BUFFER = deque(maxlen=200)

def log_activity(message, level="INFO"):
    """Log an activity message."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {
        "timestamp": timestamp,
        "message": message,
        "level": level
    }
    LOG_BUFFER.append(entry)
    print(f"[{timestamp}] {level}: {message}")

def get_logs():
    """Get recent log entries."""
    return list(LOG_BUFFER)

def clear_logs():
    """Clear the log buffer."""
    LOG_BUFFER.clear()

# ==================== VALIDATION ====================

def is_valid_email(email):
    """Validate email format."""
    if not email:
        return False
    
    # Simple robust regex
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False
    
    # Check for disposable email domains
    disposable_domains = {
        '10minutemail.com', 'temp-mail.org', 'guerrillamail.com', 'mailinator.com',
        'throwawaymail.com', 'getnada.com', 'mohmal.com', 'yopmail.com', 'maildrop.cc',
        'tempail.com', 'fakeinbox.com', 'trashmail.com'
    }
    
    try:
        domain = email.split('@')[1].lower()
        if domain in disposable_domains:
            return False
    except IndexError:
        return False
    
    return True

# ==================== FILE HANDLING ====================

def allowed_file(filename, allowed_extensions=None):
    """Check if a file extension is allowed."""
    if allowed_extensions is None:
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'csv', 'xlsx', 'txt'}
    
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

def parse_txt_file(file, campaign_id):
    """
    Parse a TXT file (one email per line) and create recipients.
    """
    from app import db
    from app.models import Recipient, Suppression
    
    added = 0
    skipped = 0
    errors = []
    
    try:
        # Read file content, handling potential encoding issues
        content = file.stream.read().decode("utf-8", errors='ignore')
        lines = content.splitlines()
        
        for row_num, line in enumerate(lines, start=1):
            email = line.strip().lower()
            
            # Skip empty lines
            if not email:
                continue
            
            # Basic validation
            if not is_valid_email(email):
                skipped += 1
                if len(errors) < 10:
                    errors.append(f"Line {row_num}: Invalid email '{email}'")
                continue
            
            # Check for duplicates in current campaign
            existing = Recipient.query.filter_by(campaign_id=campaign_id, email=email).first()
            if existing:
                skipped += 1
                continue
            
            # Check global suppression
            is_suppressed = Suppression.query.filter_by(email=email).first()
            
            recipient = Recipient(
                email=email,
                campaign_id=campaign_id,
                data=json.dumps({'email': email}), # No extra data in TXT usually
                status='Suppressed' if is_suppressed else 'Queued',
                status_message='Suppressed by global list' if is_suppressed else None
            )
            
            db.session.add(recipient)
            added += 1
            
            if added % 500 == 0:
                db.session.commit()
        
        db.session.commit()
        
        if skipped > 0:
            errors.insert(0, f"Skipped {skipped} invalid or duplicate emails")
            
        return added, errors

    except Exception as e:
        return 0, [f"Error parsing TXT file: {str(e)}"]

def parse_csv_file(file, campaign_id):
    """Parse a CSV file and create recipients."""
    from app import db
    from app.models import Recipient, Suppression
    
    try:
        stream = io.StringIO(file.stream.read().decode("UTF-8-sig"), newline=None)
        csv_reader = csv.DictReader(stream)
        
        # Normalize headers
        if csv_reader.fieldnames:
            csv_reader.fieldnames = [h.strip().lower() for h in csv_reader.fieldnames]
        
        if not csv_reader.fieldnames or 'email' not in csv_reader.fieldnames:
            return 0, ["CSV must have an 'email' column header"]
        
        added = 0
        skipped = 0
        errors = []
        
        for row_num, row in enumerate(csv_reader, start=2):
            email = row.get('email', '').strip().lower()
            
            if not email:
                continue
            
            if not is_valid_email(email):
                skipped += 1
                if len(errors) < 10:
                    errors.append(f"Row {row_num}: Invalid email '{email}'")
                continue
            
            # Check if already exists in campaign
            existing = Recipient.query.filter_by(campaign_id=campaign_id, email=email).first()
            if existing:
                skipped += 1
                continue
            
            # Check suppression
            is_suppressed = Suppression.query.filter_by(email=email).first()
            
            recipient = Recipient(
                email=email,
                campaign_id=campaign_id,
                data=json.dumps(row),
                status='Suppressed' if is_suppressed else 'Queued',
                status_message='Suppressed by global list' if is_suppressed else None
            )
            
            db.session.add(recipient)
            added += 1
            
            # Commit in batches
            if added % 500 == 0:
                db.session.commit()
        
        db.session.commit()
        
        if skipped > 0:
            errors.insert(0, f"Skipped {skipped} invalid or duplicate emails")
        
        return added, errors
    
    except Exception as e: 
        return 0, [f"Error parsing CSV: {str(e)}"]

def export_to_csv(data, headers):
    """Export data to CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    
    for row in data:
        writer.writerow(row)
    
    return output.getvalue()

# ==================== TEXT PROCESSING ====================

def html_to_plain_text(html):
    """Convert HTML to plain text."""
    if not html:
        return ""
    
    # Remove script and style elements
    text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    
    # Add newlines for block elements
    text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    
    # Remove remaining tags
    text = re.sub(r'<[^>]+>', ' ', text)
    
    # Decode HTML entities
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#39;', "'", text)
    
    # Clean up whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    lines = [line.strip() for line in text.split('\n')]
    
    return '\n'.join(line for line in lines if line)

def sanitize_filename(filename):
    """Sanitize a filename for safe storage."""
    filename = re.sub(r'[^\w\s\-\.]', '', filename)
    filename = re.sub(r'\s+', '_', filename)
    return filename[:255]
