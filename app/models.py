import re
import json
from datetime import datetime
from jinja2 import Environment, exceptions
from flask import url_for
from app.core_logic.deliverability import DeliverabilityHelper

# Common ISP domains to check against for "Company" autograb
COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com",
    "tutanota.com", "riseup.net", "disroot.org"
}

class PersonalizationEngine:
    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient
        self.jinja_env = Environment()
        self.deliverability_helper = DeliverabilityHelper()

    def _get_context(self):
        """Builds the full context dictionary for Jinja2 rendering."""
        # 1. Start with existing recipient data
        try:
            context = json.loads(self.recipient.data) if self.recipient.data else {}
        except:
            context = {}
        
        # Ensure email is in context
        context['email'] = self.recipient.email

        # 2. Autograb Firstname (if missing)
        if 'firstname' not in context or not context['firstname']:
            local_part = self.recipient.email.split('@')[0]
            # Split by common separators
            potential_parts = re.split(r'[._\-+]+', local_part)
            # Filter out non-alpha and generic words
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            generic_words = {'info', 'contact', 'admin', 'support', 'sales', 'mail', 'office', 'hello', 'enquiry'}
            
            found_name = None
            if valid_parts:
                candidate = valid_parts[0]
                if candidate.lower() not in generic_words:
                    found_name = candidate.capitalize()
            
            if found_name:
                context['firstname'] = found_name
            else:
                context['firstname'] = "there" # Fallback

        # 3. Autograb Company (if missing)
        if 'company' not in context or not context['company']:
            domain = self.recipient.email.split('@')[1].lower()
            if domain in COMMON_ISP_DOMAINS:
                context['company'] = "you" # Fallback for ISP domains
            else:
                # Extract company name from non-ISP domain
                parts = domain.split('.')
                # Logic: get second to last part if domain is like .co.uk, else first part
                company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net') else parts[0]
                context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])

        # 4. Dynamic Greetings
        now = datetime.utcnow() # Web uses UTC typically
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"
        
        # Combine greeting with name
        # If firstname is "there" (fallback), user might prefer just "Good morning" or "Good morning there"
        # We stick to the script logic:
        context['greetings'] = f"{base_greeting} {context['firstname']}"

        # 5. Other fields
        context['sender_name'] = self.campaign.smtp_profile.sender_name if self.campaign.smtp_profile else "Sender"
        context['currentdate'] = now.strftime("%B %d, %Y")
        
        # 6. Tracking Links
        # We need external URLs. Note: This requires SERVER_NAME or correct request context in Celery
        try:
            unsub_token = self.recipient.get_tracking_token('unsubscribe')
            context['unsubscribe_link'] = url_for('core_logic.unsubscribe', token=unsub_token, _external=True)
            
            open_token = self.recipient.get_tracking_token('open')
            self.open_pixel_url = url_for('core_logic.track_open', token=open_token, _external=True)
        except RuntimeError:
            # Handle cases where app context/URL generation fails in background
            context['unsubscribe_link'] = "#"
            self.open_pixel_url = "#"

        return context

    def _replace_links_for_tracking(self, html_content):
        """Replaces hrefs with tracking redirects."""
        def replace_link(match):
            original_url = match.group(2)
            # Skip unsubscribe, mailto, anchor links, or already tracked links
            if any(x in original_url for x in ['unsubscribe', 'mailto:', '#', '/track/']):
                return match.group(0)
            
            try:
                # Payload ensures we know where to redirect even if DB is slow
                click_token = self.recipient.get_tracking_token('click', payload={'url': original_url})
                tracked_url = url_for('core_logic.track_click', token=click_token, _external=True)
                return f'{match.group(1)}="{tracked_url}"'
            except:
                return match.group(0)

        return re.sub(r'(href\s*=\s*)(["\'](https?://[^"\']+)["\'])', replace_link, html_content, flags=re.IGNORECASE)

    def personalize(self):
        context = self._get_context()

        # 1. Spintax
        spun_subject = self.deliverability_helper.spin(self.campaign.subject)
        spun_body = self.deliverability_helper.spin(self.campaign.body)

        # 2. Jinja2 Rendering
        # Replace [tag] style with {{ tag }} style first for compatibility
        spun_subject = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{ \1 }}', spun_subject)
        spun_body = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{ \1 }}', spun_body)

        try:
            final_subject = self.jinja_env.from_string(spun_subject).render(context)
            final_body = self.jinja_env.from_string(spun_body).render(context)
        except exceptions.TemplateError:
            # Fallback if template syntax is wrong
            final_subject = spun_subject
            final_body = spun_body

        # 3. Tracking
        final_body = self._replace_links_for_tracking(final_body)
        
        # Add pixel
        pixel_img = f'<img src="{self.open_pixel_url}" width="1" height="1" alt="" style="display:none;"/>'
        if '</body>' in final_body.lower():
            final_body = final_body.replace('</body>', f'{pixel_img}</body>')
        else:
            final_body += pixel_img

        return final_subject, final_body
