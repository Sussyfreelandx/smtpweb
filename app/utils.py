from collections import deque
from datetime import datetime
import re
import csv
import io
import json


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
    """Validate email format and exclude common disposable domains."""
    if not email:
        return False

    email = email.strip()
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}$'
    if not re.match(pattern, email):
        return False

    disposable_domains = {
        '10minutemail.com', 'temp-mail.org', 'guerrillamail.com', 'mailinator.com',
        'throwawaymail.com', 'getnada.com', 'mohmal.com', 'yopmail.com', 'maildrop.cc',
        'tempail.com', 'fakeinbox.com', 'trashmail.com'
    }
    try:
        domain = email.split('@', 1)[1].lower()
    except Exception:
        return False
    if domain in disposable_domains:
        return False

    return True


def validate_email_list(emails):
    """Validate a list of emails and return valid/invalid lists."""
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
    Parse an uploaded recipients file. Supports:
      - CSV with header containing 'email' column (case-insensitive)
      - TXT: one email per line or CSV-like first-line header then values

    Returns (added_count, errors_list)
    """
    from app import db
    from app.models import Recipient, Suppression

    try:
        # Read entire stream as text
        raw = file.stream.read()
        if isinstance(raw, bytes):
            raw_text = raw.decode("utf-8-sig", errors='replace')
        else:
            raw_text = str(raw)

        # Normalize line endings
        raw_text = raw_text.replace('\r\n', '\n').replace('\r', '\n')
        
        # Determine filename extension if available
        filename = getattr(file, 'filename', '') or ''
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

        # First try CSV DictReader
        stream = io.StringIO(raw_text, newline=None)
        
        # Sniff delimiter if possible
        try:
            sample = raw_text[:1024]
            dialect = csv.Sniffer().sniff(sample)
            csv_reader = csv.DictReader(stream, dialect=dialect)
        except csv.Error:
            # Fallback to default
            csv_reader = csv.DictReader(stream)

        # Normalize headers if present
        if csv_reader.fieldnames:
            csv_reader.fieldnames = [h.strip().lower() if h else h for h in csv_reader.fieldnames]

        # If CSV has an 'email' column, process as CSV
        if csv_reader.fieldnames and 'email' in csv_reader.fieldnames:
            added = 0
            skipped = 0
            errors = []

            for row_num, row in enumerate(csv_reader, start=2):
                email = (row.get('email') or '').strip().lower()
                if not email:
                    skipped += 1
                    continue

                if not is_valid_email(email):
                    skipped += 1
                    if len(errors) < 10:
                        errors.append(f"Row {row_num}: Invalid email '{email}'")
                    continue

                # Check duplication in campaign
                existing = Recipient.query.filter_by(campaign_id=campaign_id, email=email).first()
                if existing:
                    skipped += 1
                    continue

                is_suppressed = Suppression.query.filter_by(email=email).first()

                # Autograb check: if firstname missing, try extracting
                if not row.get('firstname'):
                    row['firstname'] = extract_firstname_from_email(email)
                
                if not row.get('company'):
                     row['company'] = extract_company_from_domain(extract_domain(email))

                recipient = Recipient(
                    email=email,
                    campaign_id=campaign_id,
                    data=json.dumps(row),
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

        # If no CSV header or file is .txt, fall back to line-based parsing
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        
        added = 0
        skipped = 0
        errors = []
        
        for idx, line in enumerate(lines, start=1):
            email = line.strip().lower()
            # Simple check if line contains multiple comma-separated values
            if ',' in line:
                 parts = line.split(',')
                 # Heuristic: Find the part that looks like an email
                 for part in parts:
                     clean_part = part.strip()
                     if is_valid_email(clean_part):
                         email = clean_part
                         break
            
            if not is_valid_email(email):
                skipped += 1
                if len(errors) < 10:
                    errors.append(f"Line {idx}: Invalid email '{email}'")
                continue
                
            existing = Recipient.query.filter_by(campaign_id=campaign_id, email=email).first()
            if existing:
                skipped += 1
                continue
                
            is_suppressed = Suppression.query.filter_by(email=email).first()
            
            # Autograb data for TXT files too
            row_data = {
                'email': email,
                'firstname': extract_firstname_from_email(email),
                'company': extract_company_from_domain(extract_domain(email))
            }
            
            recipient = Recipient(
                email=email,
                campaign_id=campaign_id,
                data=json.dumps(row_data),
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

    # Decode HTML entities (basic)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")

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
    if not domain: return False
    return domain.lower() in COMMON_ISP_DOMAINS


def extract_company_from_domain(domain):
    """Extract company name from domain."""
    if not domain:
        return None

    domain = domain.lower()
    if domain in COMMON_ISP_DOMAINS:
        return None

    parts = domain.split('.')
    # Handle co.uk, com.au etc
    if len(parts) > 2 and parts[-2] in ('co', 'com', 'org', 'net', 'ac', 'gov', 'edu'):
        company_part = parts[-3]
    elif len(parts) >= 2:
        company_part = parts[0]
    else:
        return None

    return '-'.join(p.capitalize() for p in company_part.split('-'))


def extract_firstname_from_email(email):
    """Extract probable first name from email address."""
    if not email or '@' not in email:
        return None

    local_part = email.split('@')[0].lower()
    # Handle first.last, first_last, first-last
    potential_parts = re.split(r'[._\-+]+', local_part)
    valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]

    generic_words = {
        'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email',
        'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs',
        'careers', 'service', 'team', 'office', 'billing', 'accounts',
        'dev', 'webmaster', 'media', 'noreply', 'no-reply', 'marketing',
        'newsletter', 'updates', 'general', 'enquiry', 'staff', 'manager',
        'hr', 'recruitment', 'inquiries', 'help', 'feedback', 'postmaster',
        'finance', 'legal', 'ceo', 'director'
    }

    if not valid_parts:
        return None

    # Try the first part
    part = valid_parts[0]
    if part not in generic_words:
        return part.capitalize()
        
    return None


# ==================== TIME UTILITIES ====================

def get_greeting_by_time(hour=None):
    """Get appropriate greeting based on time of day."""
    if hour is None:
        hour = datetime.now().hour

    if 5 <= hour < 12:
        return "Good morning"
    elif 12 <= hour < 18:
        return "Good afternoon"
    else:
        return "Good evening"


def format_datetime(dt, format_str=None):
    """Format datetime object."""
    if not dt:
        return ""

    if format_str is None:
        format_str = "%Y-%m-%d %H:%M:%S"

    return dt.strftime(format_str)


def parse_datetime(dt_str, format_str=None):
    """Parse datetime string."""
    if not dt_str:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y"
    ]

    if format_str:
        formats.insert(0, format_str)

    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue

    return None


# ==================== SECURITY UTILITIES ====================

def generate_csrf_token():
    """Generate a CSRF token."""
    import secrets
    return secrets.token_hex(32)


def mask_email(email):
    """Mask an email address for display."""
    if not email or '@' not in email:
        return email

    local, domain = email.split('@')

    if len(local) <= 2:
        masked_local = local[0] + '*'
    else:
        masked_local = local[0] + '*' * (len(local) - 2) + local[-1]

    return f"{masked_local}@{domain}"


def mask_api_key(key):
    """Mask an API key for display."""
    if not key or len(key) < 12:
        return '***'

    return key[:8] + '*' * 16 + key[-4:]


# ==================== SPINTAX PROCESSING ====================

def process_spintax(text):
    """Process spintax {option1|option2|option3} in text."""
    import random

    if not text:
        return text

    pattern = re.compile(r'\{([^{}]*\|[^{}]*)\}')

    while True:
        match = pattern.search(text)
        if not match:
            break

        options = match.group(1).split('|')
        replacement = random.choice(options)
        text = text[:match.start()] + replacement + text[match.end():]

    return text


# ==================== URL UTILITIES ====================

def add_tracking_params(url, params):
    """Add tracking parameters to a URL."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)

    for key, value in params.items():
        query_params[key] = [value]

    new_query = urlencode(query_params, doseq=True)

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment
    ))


def extract_links_from_html(html):
    """Extract all href links from HTML content."""
    if not html:
        return []

    links = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    return [l for l in links if l.startswith(('http://', 'https://'))]


# ==================== RATE LIMITING ====================

class RateLimiter:
    """Simple in-memory rate limiter."""
    def __init__(self, max_requests, window_seconds):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = {}

    def is_allowed(self, key):
        now = datetime.utcnow().timestamp()

        if key not in self.requests:
            self.requests[key] = []

        self.requests[key] = [t for t in self.requests[key] if now - t < self.window_seconds]

        if len(self.requests[key]) >= self.max_requests:
            return False

        self.requests[key].append(now)
        return True

    def get_remaining(self, key):
        now = datetime.utcnow().timestamp()

        if key not in self.requests:
            return self.max_requests

        self.requests[key] = [t for t in self.requests[key] if now - t < self.window_seconds]

        return max(0, self.max_requests - len(self.requests[key]))


# ==================== ENGAGEMENT SCORING ====================

def calculate_engagement_score(recipient):
    """Calculate engagement score for a recipient."""
    score = 0.0

    if recipient.status == 'Sent':
        score += 10

    if recipient.opened_at:
        score += 20
        score += min(recipient.open_count or 0, 5) * 5

    if recipient.clicked_at:
        score += 30
        score += min(recipient.click_count or 0, 10) * 3

    if recipient.replied_at:
        score += 50

    if recipient.status == 'Bounced':
        score -= 50

    if recipient.status == 'Unsubscribed':
        score -= 100

    # Recency bonus
    if recipient.opened_at:
        days_since_open = (datetime.utcnow() - recipient.opened_at).days
        if days_since_open < 7:
            score += 10
        elif days_since_open < 30:
            score += 5

    return max(0, min(100, score))
