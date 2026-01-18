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
}

class PersonalizationEngine:
    """
    Handles all email personalization, including autograb, Jinja2 rendering,
    and tracking link insertion. Matches logic from paris_sender_complete.py v8.0.3
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
        
        # Ensure all keys are lowercase for template consistency
        context = {k.lower(): v for k, v in context.items()}
        
        # --- Autograb Logic (Matches paris_sender_complete.py) ---
        
        # 1. Firstname Autograb
        found_name = context.get('firstname')
        if not found_name:
            local_part = self.recipient.email.split('@')[0]
            # Split by dots, dashes, underscores
            potential_parts = re.split(r'[._\-+]+', local_part)
            # Filter for alphabetic parts only
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            
            generic_words = {
                'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email', 
                'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs', 
                'careers', 'service', 'team', 'office', 'billing', 'accounts'
            }
            
            if valid_parts:
                candidate = valid_parts[0]
                if candidate.lower() not in generic_words:
                    found_name = candidate.capitalize()
                    context['firstname'] = found_name

        # 2. Company Autograb
        found_company = context.get('company')
        if not found_company:
            try:
                domain = self.recipient.email.split('@')[1].lower()
                if domain in COMMON_ISP_DOMAINS:
                    # Fallback for ISP domains
                    found_company = "you" 
                else:
                    parts = domain.split('.')
                    # Basic logic: take the SLD (second level domain)
                    # e.g., company.com -> company
                    # e.g., company.co.uk -> company
                    if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net'):
                        company_part = parts[-2]
                    else:
                        company_part = parts[0]
                    
                    found_company = '-'.join([p.capitalize() for p in company_part.split('-')])
                
                context['company'] = found_company
            except:
                context['company'] = 'you'

        # --- Dynamic & Global Placeholders ---
        now = datetime.utcnow()
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"
        
        # Logic: If name exists, append it. Else just greeting.
        if context.get('firstname'):
            context['greetings'] = f"{base_greeting} {context['firstname']}"
        else:
            context['greetings'] = base_greeting
            
        context['sender_name'] = self.campaign.smtp_profile.sender_name if self.campaign.smtp_profile else "Sender"
        context['currentdate'] = now.strftime("%B %d, %Y")
        context['time'] = now.strftime("%I:%M %p")
        
        # --- Fallbacks (to prevent errors if data is missing) ---
        context.setdefault('firstname', 'Hello')
        context.setdefault('company', 'you')

        # --- Tracking Links (Generated via Flask's url_for) ---
        # Note: We need _external=True for absolute URLs
        unsubscribe_token = self.recipient.get_tracking_token('unsubscribe')
        open_token = self.recipient.get_tracking_token('open')
        
        # Matches autograb format
        context['unsubscribe_link'] = url_for('core_logic.unsubscribe', campaign_id=self.campaign.id, recipient_id=self.recipient.id, _external=True)
        self.open_pixel_url = url_for('core_logic.track_open', campaign_id=self.campaign.id, recipient_id=self.recipient.id, _external=True)
        
        return context

    def _render_with_jinja(self, template_string, context):
        """Safely renders a string using Jinja2."""
        if not template_string: return ""
        try:
            # First, handle the legacy [placeholder] syntax by converting to {{ placeholder }}
            # This matches the desktop app behavior which supports both.
            template_string = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{ \1 }}', template_string)
            
            template = self.jinja_env.from_string(template_string)
            return template.render(context)
        except exceptions.TemplateError as e:
            # If rendering fails, return original or a safe fallback
            return template_string

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
            if 'unsubscribe' in original_url or original_url.startswith(('mailto:', '#')) or 'track' in original_url:
                return match.group(0)
            
            # Encode target URL
            encoded_url = base64.urlsafe_b64encode(original_url.encode()).decode()
            
            # Generate tracking URL
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

        # 2. Render with Jinja2 using the context (handles autograb)
        final_subject = self._render_with_jinja(spun_subject, context)
        final_body = self._render_with_jinja(spun_body, context)

        # 3. Add tracking links and pixel
        body_with_tracked_links = self._replace_links_for_tracking(final_body)
        final_body_with_pixel = self._add_tracking_pixel(body_with_tracked_links)

        return final_subject, final_body_with_pixel
