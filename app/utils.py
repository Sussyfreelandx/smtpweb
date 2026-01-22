from collections import deque
from datetime import datetime
from flask import current_app
import re
import csv
import io
import json
import openpyxl  # For reading xlsx files


# ==================== LOGGING ====================

LOG_BUFFER = deque(maxlen=200)


def log_activity(message, level="INFO"):
    """Log an activity message."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"timestamp": timestamp, "message": message, "level": level}
    LOG_BUFFER.append(entry)
    print(f"[{timestamp}] {level}: {message}")


def get_logs():
    """Get recent log entries."""
    return list(LOG_BUFFER)


# ==================== VALIDATION ====================

def is_valid_email(email):
    """Validate email format and check against a list of disposable domains."""
    if not email or not isinstance(email, str):
        return False
    
    # Basic regex for email format
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False
    
    # Check for disposable email domains
    disposable_domains = {
        '10minutemail.com', 'temp-mail.org', 'guerrillamail.com', 'mailinator.com',
        'throwawaymail.com', 'getnada.com', 'mohmal.com', 'yopmail.com',
    }
    
    try:
        domain = email.split('@')[1].lower()
        if domain in disposable_domains:
            return False
    except IndexError:
        return False
    
    return True


# ==================== FILE HANDLING & RECIPIENT IMPORTING ====================

class RecipientImporter:
    """
    A robust class to handle importing recipients from various file types (CSV, TXT, XLSX).
    """
    def __init__(self, file_storage, campaign_id):
        self.file = file_storage
        self.campaign_id = campaign_id
        self.filename = file_storage.filename.lower()
        from app import db
        from app.models import Recipient, Suppression
        self.db = db
        self.Recipient = Recipient
        self.Suppression = Suppression

    def parse(self):
        """Parse the file based on its extension."""
        if self.filename.endswith('.csv'):
            return self._parse_csv()
        elif self.filename.endswith('.txt'):
            return self._parse_txt()
        elif self.filename.endswith('.xlsx'):
            return self._parse_xlsx()
        else:
            return 0, ["Unsupported file type. Please upload a CSV, TXT, or XLSX file."]

    def _add_recipient(self, email, data_dict):
        """Helper to add a single recipient to the database session."""
        email = email.strip().lower()
        if not is_valid_email(email):
            return 'skipped_invalid'

        # Check if already exists in campaign
        if self.Recipient.query.filter_by(campaign_id=self.campaign_id, email=email).first():
            return 'skipped_duplicate'
        
        # Check suppression list
        is_suppressed = self.Suppression.query.filter_by(email=email).first()
        
        recipient = self.Recipient(
            email=email,
            campaign_id=self.campaign_id,
            data=json.dumps(data_dict),
            status='Suppressed' if is_suppressed else 'Queued'
        )
        self.db.session.add(recipient)
        return 'added'

    def _parse_csv(self):
        """Parse a CSV file."""
        added, skipped = 0, 0
        errors = []
        try:
            stream = io.StringIO(self.file.stream.read().decode("UTF-8-sig"), newline=None)
            csv_reader = csv.DictReader(stream)
            
            if not csv_reader.fieldnames or 'email' not in [h.lower().strip() for h in csv_reader.fieldnames]:
                return 0, ["CSV must have an 'email' column header."]
            
            # Normalize headers
            csv_reader.fieldnames = [h.strip().lower() for h in csv_reader.fieldnames]

            for row_num, row in enumerate(csv_reader, start=2):
                email = row.get('email', '').strip()
                if not email:
                    continue
                
                result = self._add_recipient(email, row)
                if result == 'added':
                    added += 1
                else:
                    skipped += 1
                
                if added > 0 and added % 500 == 0:
                    self.db.session.commit()
            
            self.db.session.commit()

        except Exception as e:
            self.db.session.rollback()
            errors.append(f"Error parsing CSV: {e}")
        
        if skipped > 0:
            errors.insert(0, f"Skipped {skipped} invalid or duplicate emails.")
            
        return added, errors

    def _parse_txt(self):
        """Parse a TXT file (one email per line)."""
        added, skipped = 0, 0
        errors = []
        try:
            content = self.file.stream.read().decode("UTF-8-sig")
            lines = content.splitlines()

            for line in lines:
                email = line.strip()
                if not email:
                    continue
                
                result = self._add_recipient(email, {'email': email})
                if result == 'added':
                    added += 1
                else:
                    skipped += 1
            
            self.db.session.commit()

        except Exception as e:
            self.db.session.rollback()
            errors.append(f"Error parsing TXT file: {e}")

        if skipped > 0:
            errors.insert(0, f"Skipped {skipped} invalid or duplicate emails.")
            
        return added, errors

    def _parse_xlsx(self):
        """Parse an XLSX (Excel) file."""
        added, skipped = 0, 0
        errors = []
        try:
            workbook = openpyxl.load_workbook(self.file.stream)
            sheet = workbook.active
            
            headers = [cell.value.lower().strip() for cell in sheet[1]]
            if 'email' not in headers:
                return 0, ["XLSX file must have an 'email' column header in the first row."]
            
            email_col_index = headers.index('email')

            for row_num, row in enumerate(sheet.iter_rows(min_row=2), start=2):
                email_cell = row[email_col_index]
                email = str(email_cell.value).strip() if email_cell.value else ''
                
                if not email:
                    continue

                row_data = {headers[i]: str(cell.value) for i, cell in enumerate(row)}
                result = self._add_recipient(email, row_data)
                
                if result == 'added':
                    added += 1
                else:
                    skipped += 1

                if added > 0 and added % 500 == 0:
                    self.db.session.commit()
            
            self.db.session.commit()

        except Exception as e:
            self.db.session.rollback()
            errors.append(f"Error parsing XLSX file: {e}")
        
        if skipped > 0:
            errors.insert(0, f"Skipped {skipped} invalid or duplicate emails.")
            
        return added, errors


def allowed_file(filename):
    """Check if a file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'csv', 'txt', 'xlsx'}
