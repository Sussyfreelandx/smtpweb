import re
import base64
import uuid
import json
from datetime import datetime
from jinja2 import Environment, exceptions
from flask import url_for
from app.core_logic.deliverability import DeliverabilityHelper

# These domains are used to determine if the company name should be a fallback.
COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com",
    "tutanota.com", "riseup.net", "disroot.org", "me.com", "mac.com"
}

GENERIC_WORDS = {
    'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email', 
    'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs', 
    'careers', 'service', 'team', 'office', 'billing', 'accounts', 
    'dev', 'webmaster', 'media', 'noreply', 'no-reply', 'marketing', 
    'newsletter', 'updates', 'general', 'enquiry', 'staff', 'manager', 
    'hr', 'recruitment', 'inquiries'
}

class PersonalizationEngine:
    """
    Handles all email personalization, including autograb, Jinja2 rendering,
    and tracking link insertion. Matches Desktop v8.0.3 logic.
    """

    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient
        self.jinja_env = Environment()
        self.deliverability_helper = DeliverabilityHelper()

    def _get_context(self):
        """Builds the full context dictionary for Jinja2 rendering."""
        # Start with the recipient's own data (from CSV)
        try:
            context = json.loads(self.recipient.data) if self.recipient.data else {}
        except:
            context = {}
        
        # Ensure lowercase keys for consistency
        context = {k.lower(): v for k, v in context.items()}
        
        # --- Autograb Logic (Matches Desktop v8.0.3) ---
        
        # 1. Firstname Autograb
        if 'firstname' not in context or not context['firstname']:
            local_part = self.recipient.email.split('@')[0]
            # Split by common delimiters (dot, underscore, hyphen, plus)
            potential_parts = re.split(r'[._\-+]+', local_part)
            # Filter for alphabetic parts containing > 1 char
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            
            if valid_parts:
                candidate = valid_parts[0]
                if candidate.lower() not in GENERIC_WORDS:
                    context['firstname'] = candidate.capitalize()
        
        # Fallback for Firstname
        if 'firstname' not in context or not context['firstname']:
            context['firstname'] = 'Hello' # Or empty string depending on preference

        # 2. Company Autograb
        if 'company' not in context or not context['company']:
            try:
                domain = self.recipient.email.split('@')[1].lower()
                if domain in COMMON_ISP_DOMAINS:
                    context['company'] = 'you' # Fallback for ISP domains
                else:
                    # Extract company name from non-ISP domain
                    parts = domain.split('.')
                    # Heuristic: Take the domain part, avoiding TLDs like .co.uk
                    company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'ac', 'gov', 'edu') else parts[0]
                    context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])
            except:
                context['company'] = 'you'

        # --- Dynamic & Global Placeholders ---
        now = datetime.utcnow()
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"
        
        # Re-construct greeting based on found name
        if context.get('firstname') and context['firstname'] != 'Hello':
            context['greetings'] = f"{base_greeting} {context['firstname']}"
        else:
            context['greetings'] = base_greeting

        context['sender_name'] = self.campaign.smtp_profile.sender_name if self.campaign.smtp_profile else "Sender"
        context['currentdate'] = now.strftime("%B %d, %Y")
        context['time'] = now.strftime("%I:%M %p")
        
        # --- Tracking Links ---
        unsubscribe_token = self.recipient.get_tracking_token('unsubscribe')
        open_token = self.recipient.get_tracking_token('open')
        
        # Generate URLs
        context['unsubscribe_link'] = url_for('core_logic.unsubscribe', campaign_id=self.campaign.id, recipient_id=self.recipient.id, _external=True)
        self.open_pixel_url = url_for('core_logic.track_open', campaign_id=self.campaign.id, recipient_id=self.recipient.id, _external=True)
        
        return context

    def _render_with_jinja(self, template_string, context):
        """Safely renders a string using Jinja2."""
        # Pre-process: Replace legacy [tag] syntax with {{ tag }}
        processed_string = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{ \1 }}', template_string)
        
        try:
            template = self.jinja_env.from_string(processed_string)
            return template.render(context)
        except exceptions.TemplateError:
            # Fallback manual replacement if Jinja fails
            for k, v in context.items():
                processed_string = processed_string.replace(f"{{{{ {k} }}}}", str(v))
            return processed_string

    def _add_tracking_pixel(self, html_content):
        """Injects the 1x1 tracking pixel before the closing </body> tag."""
        pixel_img = f'<img src="{self.open_pixel_url}" width="1" height="1" alt="" border="0" style="height:1px;width:1px;border:0;"/>'
        if '</body>' in html_content.lower():
            return html_content.replace('</body>', f'{pixel_img}</body>', 1)
        return html_content + pixel_img

    def _replace_links_for_tracking(self, html_content):
        """Replaces all hrefs with a trackable redirect link."""
        def replace_link(match):
            original_url = match.group(2)
            # Don't track unsubscribe links or mailto links
            if 'unsubscribe' in original_url or original_url.startswith(('mailto:', '#')) or '/track/' in original_url:
                return match.group(0)
            
            # Create a token specific to this link and recipient
            encoded_url = base64.urlsafe_b64encode(original_url.encode()).decode()
            tracked_url = url_for('core_logic.track_click', campaign_id=self.campaign.id, recipient_id=self.recipient.id, url=encoded_url, _external=True)
            return f'{match.group(1)}="{tracked_url}"'

        return re.sub(r'(href\s*=\s*)(["\'](https?://[^"\']+)["\'])', replace_link, html_content, flags=re.IGNORECASE)

    def personalize(self):
        """
        Runs the full personalization and tracking pipeline.
        Returns the final subject and HTML body.
        """
        context = self._get_context()

        # 1. Spin the text first (spintax)
        spun_subject = self.deliverability_helper.spin(self.campaign.subject)
        spun_body = self.deliverability_helper.spin(self.campaign.body)

        # 2. Render with Jinja2 using the context
        final_subject = self._render_with_jinja(spun_subject, context)
        final_body = self._render_with_jinja(spun_body, context)

        # 3. Add tracking links and pixel
        body_with_tracked_links = self._replace_links_for_tracking(final_body)
        final_body_with_pixel = self._add_tracking_pixel(body_with_tracked_links)

        return final_subject, final_body_with_pixel
