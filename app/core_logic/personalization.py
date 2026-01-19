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

from app.models import GlobalSettings

COMMON_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "msn.com", "live.com", "icloud.com", "mail.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "yandex.com",
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
    """
    Advanced Personalization Engine (Web Version)
    Features:  Autograb, A/B Testing, Spintax, Secure Links, Inline CSS. 
    """

    def __init__(self, campaign, recipient):
        self.campaign = campaign
        self.recipient = recipient

    def _get_context(self):
        """
        Builds the context dictionary for template rendering.
        Implements autograb for firstname, company, greetings. 
        """
        try:
            context = json.loads(self.recipient.data) if self.recipient.data else {}
        except json.JSONDecodeError:
            context = {}
        
        context = {k. lower(): v for k, v in context.items()}
        
        # --- AUTOGRAB:  Firstname ---
        if 'firstname' not in context or not context['firstname']:
            local_part = self.recipient.email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p. isalpha()]
            
            if valid_parts and valid_parts[0]. lower() not in GENERIC_EMAIL_WORDS:
                context['firstname'] = valid_parts[0]. capitalize()
            else:
                context['firstname'] = "there"

        # --- AUTOGRAB:  Company ---
        if 'company' not in context or not context['company']:
            try:
                domain = self.recipient.email.split('@')[1]. lower()
                if domain in COMMON_ISP_DOMAINS: 
                    context['company'] = "your company"
                else:
                    parts = domain.split('.')
                    company_part = parts[-2] if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'gov', 'edu') else parts[0]
                    context['company'] = '-'.join([p.capitalize() for p in company_part.split('-')])
            except IndexError:
                context['company'] = "your company"

        # --- DYNAMIC PLACEHOLDERS ---
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12:
            base_greeting = "Good morning"
        elif 12 <= hour < 18:
            base_greeting = "Good afternoon"
        else: 
            base_greeting = "Good evening"
        
        context['greetings'] = f"{base_greeting} {context['firstname']}"
        context['currentdate'] = now.strftime("%B %d, %Y")
        context['time'] = now.strftime("%I:%M %p")
        context['sender_name'] = self.campaign.smtp_profile.sender_name if self.campaign.smtp_profile else "Sender"
        context['sender_email'] = self.campaign.smtp_profile.sender_email if self.campaign.smtp_profile else ""

        # {{ secure_link }}
        context['secure_link'] = self._generate_secure_link()

        # {{ unsubscribe_link }}
        unsubscribe_token = self. recipient.get_tracking_token('unsubscribe')
        context['unsubscribe_link'] = url_for('main.unsubscribe', token=unsubscribe_token, _external=True)
        
        return context

    def _generate_secure_link(self):
        """Generates a secure, unique link."""
        burner_domain = self.campaign.burner_domain
        lure_path = self.campaign.lure_path
        
        if not burner_domain or not lure_path:
            global_settings = GlobalSettings.query. first()
            if global_settings:
                
