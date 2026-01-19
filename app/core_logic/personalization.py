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
    "msn. com", "live.com", "icloud.com", "mail.com", "comcast.net",
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
            local_part = self.recipient.email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p
