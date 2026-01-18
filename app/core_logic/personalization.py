import re
import base64
import uuid
from datetime import datetime
from jinja2 import Environment, exceptions
from flask import url_for
from .deliverability import DeliverabilityHelper

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
    and tracking link insertion.
    """

    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient
        self.jinja_env = Environment()
        self.deliverability_helper = DeliverabilityHelper()

    def _get_context(self):
        """Builds the full context dictionary for Jinja2 rendering."""
        # Start with the recipient's own data (from CSV)
        context = self.recipient.get_data()
        
        # --- Autograb Logic ---
        # 1. Firstname
        if 'firstname' not in context or not context['firstname']:
            local_part = self.recipient.email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            generic_words = {'info', 'contact', 'admin', 'support', 'sales', 'mail'}
            if valid_parts and valid_parts[0].lower() not in generic_words:
                context['firstname'] = valid_parts[0].capitalize()
        
        # 2. Company
        if 'company' not in context or not context['company']:
            domain = self.recipient.email.split('@')[1].lower()
            if domain not in COMMON_ISP_DOMAINS:
                parts = domain.split('.')
                company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 else parts[0]
                context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])

        # --- Dynamic & Global Placeholders ---
        now = datetime.utcnow()
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"
        
        context['greetings'] = f"{base_greeting} {context['firstname']}" if context.get('firstname') else base_greeting
        context['sender_name'] = self.campaign.smtp_sender_name
        context['currentdate'] = now.strftime("%B %d, %Y")
        
        # --- Fallbacks (to prevent errors if data is missing) ---
        context.setdefault('firstname', 'there')
        context.setdefault('company', 'your team')

        # --- Tracking Links (Generated via Flask's url_for) ---
        # We generate a signed token to securely identify the recipient
        unsubscribe_token = self.recipient.get_tracking_token('unsubscribe')
        open_token = self.recipient.get_tracking_token('open')
        
        context['unsubscribe_link'] = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
        self.open_pixel_url = url_for('main.track_open', token=open_token, _external=True)
        
        return context

    def _render_with_jinja(self, template_string, context):
        """Safely renders a string using Jinja2."""
        try:
            template = self.jinja_env.from_string(template_string)
            return template.render(context)
        except exceptions.TemplateError as e:
            # If rendering fails, return the original string to avoid breaking the send
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
            if 'unsubscribe' in original_url or original_url.startswith(('mailto:', '#')):
                return match.group(0)
            
            # Create a token specific to this link and recipient
            click_token = self.recipient.get_tracking_token('click', payload={'url': original_url})
            tracked_url = url_for('main.track_click', token=click_token, _external=True)
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
        spun_body = self.deliverability_helper.spin(self.campaign.body_html)

        # 2. Render with Jinja2 using the context
        final_subject = self._render_with_jinja(spun_subject, context)
        final_body = self._render_with_jinja(spun_body, context)

        # 3. Add tracking links and pixel
        body_with_tracked_links = self._replace_links_for_tracking(final_body)
        final_body_with_pixel = self._add_tracking_pixel(body_with_tracked_links)

        return final_subject, final_body_with_pixel
