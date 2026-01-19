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
    "verizon.net", "att. net", "sbcglobal.net", "cox.net", "yandex.com",
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

    def _get_context(self):
        try:
            context = json.loads(self.recipient.data) if self.recipient.data else {}
        except json.JSONDecodeError:
            context = {}

        context = {k. lower(): v for k, v in context.items()}

        # AUTOGRAB:  Firstname
        if 'firstname' not in context or not context. get('firstname'):
            local_part = self.recipient.email.split('@')[0]
            potential_parts = re.split(r'[._\-+]+', local_part)
            valid_parts = [p for p in potential_parts if len(p) > 1 and p. isalpha()]

            if valid_parts and valid_parts[0]. lower() not in GENERIC_EMAIL_WORDS:
                context['firstname'] = valid_parts[0]. capitalize()
            else:
                context['firstname'] = "there"

        # AUTOGRAB: Company
        if 'company' not in context or not context.get('company'):
            try:
                domain = self.recipient.email.split('@')[1]. lower()
                if domain in COMMON_ISP_DOMAINS: 
                    context['company'] = "your company"
                else:
                    parts = domain.split('.')
                    if len(parts) > 2 and len(parts[-2]) > 2 and parts[-2] not in ('co', 'com', 'org', 'net', 'gov', 'edu'):
                        company_part = parts[-2]
                    else:
                        company_
