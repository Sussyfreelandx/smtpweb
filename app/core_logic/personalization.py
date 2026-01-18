import re
from datetime import datetime
from urllib.parse import urlparse, urlunparse
import base64
from jinja2 import Environment, exceptions
from flask import url_for

class PersonalizationEngine:
    """Handles Jinja2 templating and the autograb personalization logic."""

    def __init__(self, recipient_data, sender_name, base_url):
        self.recipient_data = {k.lower(): v for k, v in recipient_data.items()}
        self.sender_name = sender_name
        self.base_url = base_url
        self.jinja_env = Environment()
        self.common_isp_domains = {
            "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
            "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
            "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
            "protonmail.com", "zoho.com", "gmx.com"
        }

    def _get_full_context(self):
        """Builds the complete context dictionary for Jinja2 rendering."""
        context = self.recipient_data.copy()
        email = context.get('email', '')

        # 1. Autograb Firstname
        if 'firstname' not in context or not context['firstname']:
            local_part = email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            generic_words = {'info', 'contact', 'admin', 'support', 'sales', 'mail'}
            if valid_parts and valid_parts[0].lower() not in generic_words:
                context['firstname'] = valid_parts[0].capitalize()

        # 2. Autograb Company
        if 'company' not in context or not context['company']:
            domain = email.split('@')[1].lower()
            if domain in self.common_isp_domains:
                context['company'] = "your company"
            else:
                parts = domain.split('.')
                company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net') else parts[0]
                context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])

        # 3. Dynamic fields
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"

        # Combine greeting with firstname if available
        firstname = context.get('firstname')
        context['greetings'] = f"{base_greeting} {firstname}" if firstname else base_greeting
        
        # Add fallbacks
        context.setdefault('firstname', 'there')
        context.setdefault('company', 'your company')

        # Other dynamic fields
        context['sender_name'] = self.sender_name or "The Team"
        context['currentdate'] = now.strftime("%B %d, %Y")
        
        return context

    def personalize(self, text):
        """Renders a string (subject or body) with the full context."""
        context = self._get_full_context()
        try:
            template = self.jinja_env.from_string(text)
            return template.render(context)
        except exceptions.TemplateError as e:
            # Log this error in a real app
            print(f"Jinja2 Render Error: {e}. Placeholders may not be filled.")
            return text # Return original text on failure

    def get_unsubscribe_url(self, campaign_id, recipient_id):
        return url_for('core_logic.unsubscribe', campaign_id=campaign_id, recipient_id=recipient_id, _external=True)

    def add_tracking_to_content(self, html_content, campaign_id, recipient_id):
        """Adds tracking pixel and rewrites links for tracking."""
        # Tracking pixel
        pixel_url = url_for('core_logic.track_open', campaign_id=campaign_id, recipient_id=recipient_id, _external=True)
        pixel = f'<img src="{pixel_url}" width="1" height="1" alt="" border="0" style="display:none;">'
        if '</body>' in html_content.lower():
            html_content = re.sub(r'</body>', f'{pixel}</body>', html_content, flags=re.IGNORECASE)
        else:
            html_content += pixel

        # Link tracking
        def replace_link(match):
            original_url = match.group(2)
            if original_url.startswith(('http', 'https')) and '/track/' not in original_url and 'unsubscribe' not in original_url:
                encoded_url = base64.urlsafe_b64encode(original_url.encode()).decode()
                click_url = url_for('core_logic.track_click', campaign_id=campaign_id, recipient_id=recipient_id, url=encoded_url, _external=True)
                return f'{match.group(1)}="{click_url}"'
            return match.group(0) # Return unchanged if it's not a trackable link
        
        return re.sub(r'(href\s*=\s*)(["\'](https?://[^"\']+)["\'])', replace_link, html_content, flags=re.IGNORECASE)