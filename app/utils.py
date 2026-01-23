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
    
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
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
    except Exception:
        return False

    if domain in disposable_domains:
        return False
    
    return True


def validate_email_list(emails):
    """Validate a list of emails and return valid/invalid counts."""
    valid = []
    invalid = []
    
    for email in emails:
        email = email.strip().lower()
        if is_valid_email(email):
            valid.append(email)
        else:
            invalid.append(email)
    
    return valid, invalid


# ==================== FILE HANDLING ====================

def allowed_file(filename, allowed_extensions=None):
    """Check if a file extension is allowed."""
    if allowed_extensions is None:
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'csv', 'xlsx', 'txt'}
    
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions


def parse_csv_file(file, campaign_id):
    """
    Parse a CSV or TXT file and create recipients.
    Supports:
    - CSV files with an 'email' column (and optional other fields)
    - TXT files with one email per line (or comma-separated values where first row contains header)
    Returns (added_count, errors_list)
    """
    from app import db
    from app.models import Recipient, Suppression

    try:
        filename = getattr(file, 'filename', '') or ''
        name_lower = filename.lower()
        content_bytes = file.stream.read()
        text = content_bytes.decode("UTF-8-sig", errors="replace")
        
        # Decide CSV vs TXT style
        # If file is .csv or header contains 'email' treat as CSV
        split_lines = text.splitlines()
        first_line = split_lines[0].lower() if split_lines else ""
        
        is_csv = name_lower.endswith('.csv') or (',' in first_line and 'email' in first_line)
        
        if is_csv:
            stream = io.StringIO(text, newline=None)
            csv_reader = csv.DictReader(stream)
            
            # Normalize headers
            if csv_reader.fieldnames:
                csv_reader.fieldnames = [h.strip().lower() for h in csv_reader.fieldnames]
            
            if not csv_reader.fieldnames or 'email' not in csv_reader.fieldnames:
                return 0, ["CSV must have an 'email' column header"]
            
            rows_iter = csv_reader
            get_email_from_row = lambda row: (row.get('email') or '').strip()
            row_to_data = lambda row: {k: v for k, v in row.items()}
        else:
            # TXT style: each line an email or "email,firstname,company"
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # If first line appears to be header with email, treat as CSV
            if lines and (',' in lines[0] and 'email' in lines[0].lower()):
                stream = io.StringIO(text, newline=None)
                csv_reader = csv.DictReader(stream)
                if csv_reader.fieldnames:
                    csv_reader.fieldnames = [h.strip().lower() for h in csv_reader.fieldnames]
                if 'email' not in csv_reader.fieldnames:
                    return 0, ["CSV/TXT header must include 'email' column"]
                rows_iter = csv_reader
                get_email_from_row = lambda row: (row.get('email') or '').strip()
                row_to_data = lambda row: {k: v for k, v in row.items()}
            else:
                # Plain list of emails
                rows_iter = [{'email': l} for l in lines]
                get_email_from_row = lambda row: (row.get('email') or '').strip()
                row_to_data = lambda row: {'email': row.get('email', '').strip()}

        added = 0
        skipped = 0
        errors = []
        batch_commit_size = 500
        to_commit = 0

        for row_num, row in enumerate(rows_iter, start=2):
            email = get_email_from_row(row)
            if not email:
                continue
            email = email.strip().lower()
            
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
            
            try:
                data = row_to_data(row)
            except Exception:
                data = {'email': email}
            
            recipient = Recipient(
                email=email,
                campaign_id=campaign_id,
                data=json.dumps(data),
                status='Suppressed' if is_suppressed else 'Queued',
                status_message='Suppressed by global list' if is_suppressed else None
            )
            
            db.session.add(recipient)
            added += 1
            to_commit += 1
            
            # Commit in batches to keep transaction size small
            if to_commit >= batch_commit_size:
                db.session.commit()
                to_commit = 0
        
        if to_commit > 0:
            db.session.commit()
        
        if skipped > 0:
            errors.insert(0, f"Skipped {skipped} invalid or duplicate emails")
        
        return added, errors
    
    except Exception as e:
        return 0, [f"Error parsing file: {str(e)}"]


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


def truncate_text(text, max_length, suffix='...'):
    """Truncate text to a maximum length."""
    if not text or len(text) <= max_length:
        return text
    
    return text[:max_length - len(suffix)] + suffix


def sanitize_filename(filename):
    """Sanitize a filename for safe storage."""
    # Remove or replace unsafe characters
    filename = re.sub(r'[^\w\s\-\.]', '', filename)
    filename = re.sub(r'\s+', '_', filename)
    return filename[:255]


# ==================== DOMAIN UTILITIES ====================

COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com",
    "tutanota.com", "riseup.net", "disroot.org", "mail.ru", "qq.com"
}


def extract_domain(email):
    """Extract domain from email address."""
    if not email or '@' not in email:
        return None
    return email.split('@')[1].lower()


def is_isp_domain(domain):
    """Check if a domain is a common ISP/consumer domain."""
    return domain.lower() in COMMON_ISP_DOMAINS


def extract_company_from_domain(domain):
    """Extract company name from domain."""
    if not domain:
        return None
    
    domain = domain.lower()
    
    if domain in COMMON_ISP_DOMAINS:
        return None
    
    # Remove common TLDs
    parts = domain.split('.')
    if len(parts) > 2 and parts[-2] in ('co', 'com', 'org', 'net', 'ac', 'gov', 'edu'):
        company_part = parts[-3]
    else:
        company_part = parts[0]
    
    # Capitalize properly
    return '-'.join(p.capitalize() for p in company_part.split('-'))


def extract_firstname_from_email(email):
    """Extract probable first name from email address."""
    if not email or '@' not in email:
        return None
    
    local_part = email.split('@')[0].lower()
    
    # Split by common separators
    potential_parts = re.split(r'[._\-+]+', local_part)
    
    # Filter valid name parts
    valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
    
    # Generic words to exclude
    generic_words = {
        'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email',
        'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs',
        'careers', 'service', 'team', 'office', 'billing', 'accounts',
        'dev', 'webmaster', 'media', 'noreply', 'no-reply', 'marketing',
        'newsletter', 'updates', 'general', 'enquiry', 'staff', 'manager',
        'hr', 'recruitment', 'inquiries', 'help', 'feedback', 'postmaster'
    }
    
    for part in valid_parts:
        if part not in generic_words:
            return part.capitalize()
    
    return None
