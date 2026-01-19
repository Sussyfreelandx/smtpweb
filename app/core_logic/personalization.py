import re
import json
import random
import uuid
import css_inline
from datetime import datetime
from jinja2 import Environment, exceptions
from flask import url_for
from urllib.parse import urlparse, urlunparse
from app.core_logic.deliverability import DeliverabilityHelper
from app.models import GlobalSettings

COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com",
    "tutanota.com", "riseup.net", "disroot.org"
}

class PersonalizationEngine:
    """
    Advanced Personalization Engine (Web Version)
    Features: Autograb, A/B Testing, Spintax, Secure Links, Inline CSS.
    """

    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient
        self.jinja_env = Environment()
        self.deliverability_helper = DeliverabilityHelper()

    def _get_context(self):
        """
        Builds the context dictionary for Jinja2 rendering.
        """
        # 1. Load Recipient Data (CSV Data)
        try:
            context = json.loads(self.recipient.data) if self.recipient.data else {}
        except json.JSONDecodeError:
            context = {}
        
        # Ensure keys are lowercase for consistency
        context = {k.lower(): v for k, v in context.items()}
        
        # --- AUTOGRAB LOGIC ---
        
        # Autograb: Firstname
        if 'firstname' not in context or not context['firstname']:
            local_part = self.recipient.email.split('@')[0]
            # Split by common delimiters (., _, -)
            potential_parts = re.split(r'[._\-+]+', local_part)
            # Filter out short parts or numbers
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]
            
            generic_words = {
                'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email', 
                'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs', 
                'careers', 'service', 'team', 'office', 'billing', 'accounts', 
                'dev', 'webmaster', 'media', 'noreply', 'marketing'
            }
            
            if valid_parts and valid_parts[0].lower() not in generic_words:
                context['firstname'] = valid_parts[0].capitalize()
            else:
                context['firstname'] = "there" # Default Fallback

        # Autograb: Company
        if 'company' not in context or not context['company']:
            try:
                domain = self.recipient.email.split('@')[1].lower()
                if domain in COMMON_ISP_DOMAINS:
                    # Fallback for ISP domains
                    context['company'] = "you" 
                else:
                    parts = domain.split('.')
                    # Heuristic: avoid TLDs
                    company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'gov', 'edu') else parts[0]
                    context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])
            except IndexError:
                context['company'] = "your company"

        # --- DYNAMIC PLACEHOLDERS ---
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12: base_greeting = "Good morning"
        elif 12 <= hour < 18: base_greeting = "Good afternoon"
        else: base_greeting = "Good evening"
        
        context['greetings'] = f"{base_greeting} {context['firstname']}"
        context['currentdate'] = now.strftime("%B %d, %Y")
        context['time'] = now.strftime("%I:%M %p")
        context['sender_name'] = self.campaign.smtp_profile.sender_name if self.campaign.smtp_profile else "Sender"

        # {{ secure_link }} placeholder (Secure Redirector)
        context['secure_link'] = self._generate_secure_link()

        # {{ unsubscribe_link }} placeholder
        unsubscribe_token = self.recipient.get_tracking_token('unsubscribe')
        context['unsubscribe_link'] = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
        
        return context

    def _generate_secure_link(self):
        """
        Generates a secure, unique link.
        Priority: 
        1. Campaign Specific Settings
        2. Global Settings
        3. Fallback (#)
        """
        burner_domain = self.campaign.burner_domain
        lure_path = self.campaign.lure_path
        
        # If campaign specific settings are empty, check global settings
        if not burner_domain or not lure_path:
            global_settings = GlobalSettings.query.first()
            if global_settings:
                if not burner_domain: 
                    burner_domain = global_settings.burner_domain
                if not lure_path: 
                    lure_path = global_settings.lure_path
        
        if not burner_domain:
            return "#" # Fallback if not configured anywhere
            
        parsed_domain = urlparse(burner_domain)
        if not parsed_domain.scheme:
            burner_domain = "https://" + burner_domain
            parsed_domain = urlparse(burner_domain)

        nonce = str(uuid.uuid4())
        target_id = self.recipient.email 
        
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

    def _select_ab_content(self):
        """
        Determines whether to use Version A or Version B based on campaign settings
        and a deterministic hash of the recipient email.
        """
        if not self.campaign.ab_testing_enabled:
            return self.campaign.subject, self.campaign.body

        email_hash = hash(self.recipient.email) % 100
        split_ratio = self.campaign.ab_split_ratio or 50

        if email_hash < split_ratio:
            # Version A
            return self.campaign.subject, self.campaign.body
        else:
            # Version B
            subj = self.campaign.subject_b if self.campaign.subject_b else self.campaign.subject
            body = self.campaign.body_b if self.campaign.body_b else self.campaign.body
            return subj, body

    def _render_with_jinja(self, template_string, context):
        """Safely renders a string using Jinja2."""
        try:
            # Convert legacy [placeholder] to {{ placeholder }}
            converted_template = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{ \1 }}', template_string)
            template = self.jinja_env.from_string(converted_template)
            return template.render(context)
        except exceptions.TemplateError:
            return template_string

    def _inline_css(self, html_content):
        """Uses css_inline to make styles email-compatible."""
        try:
            inliner = css_inline.CSSInliner()
            return inliner.inline(html_content)
        except Exception:
            return html_content

    def _add_tracking_pixel(self, html_content):
        """Injects the 1x1 tracking pixel."""
        open_token = self.recipient.get_tracking_token('open')
        open_url = url_for('main.track_open', token=open_token, _external=True)
        pixel_img = f'<img src="{open_url}" width="1" height="1" alt="" border="0" style="height:1px;width:1px;border:0;display:none;"/>'
        
        if '</body>' in html_content.lower():
            return html_content.replace('</body>', f'{pixel_img}</body>', 1)
        return html_content + pixel_img

    def _replace_links_for_tracking(self, html_content):
        """Replaces hrefs with Flask tracking endpoints."""
        def replace_link(match):
            original_url = match.group(2)
            # Skip mailto, hash links, unsubscribe links, and already tracked links
            if any(x in original_url for x in ['mailto:', '#', 'unsubscribe', '/track/']):
                return match.group(0)
            
            click_token = self.recipient.get_tracking_token('click', payload={'url': original_url})
            tracked_url = url_for('main.track_click', token=click_token, _external=True)
            return f'{match.group(1)}="{tracked_url}"'

        return re.sub(r'(href\s*=\s*)(["\'](https?://[^"\']+)["\'])', replace_link, html_content, flags=re.IGNORECASE)

    def personalize(self):
        """
        Main Pipeline:
        1. Select A/B Version
        2. Spin Text (Spintax)
        3. Autograb & Context Building
        4. Jinja2 Rendering
        5. CSS Inlining
        6. Tracking Injection
        """
        # 1. Select Content (A vs B)
        raw_subject, raw_body = self._select_ab_content()
        
        # 2. Spintax
        spun_subject = self.deliverability_helper.spin(raw_subject)
        spun_body = self.deliverability_helper.spin(raw_body)
        
        # 3. Context
        context = self._get_context()
        
        # 4. Render
        final_subject = self._render_with_jinja(spun_subject, context)
        final_body = self._render_with_jinja(spun_body, context)
        
        # 5. Inline CSS
        final_body = self._inline_css(final_body)
        
        # 6. Tracking
        final_body = self._replace_links_for_tracking(final_body)
        final_body = self._add_tracking_pixel(final_body)

        return final_subject, final_body
