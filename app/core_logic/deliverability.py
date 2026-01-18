import re
import random
import logging
import requests
from flask import current_app

try:
    import dns.resolver
    DNSPYTHON_AVAILABLE = True
except ImportError:
    DNSPYTHON_AVAILABLE = False

log = logging.getLogger(__name__)

class DeliverabilityHelper:
    """Tools for improving deliverability and effectiveness, adapted for web."""

    def __init__(self):
        if not DNSPYTHON_AVAILABLE:
            self.resolver = None
            log.warning("dnspython not installed. Deliverability checks are disabled.")
        else:
            self.resolver = dns.resolver.Resolver()
            self.resolver.timeout = 3
            self.resolver.lifetime = 3

        self.blacklist_servers = [
            "bl.spamcop.net", "dnsbl.sorbs.net", "zen.spamhaus.org",
            "b.barracudacentral.org", "cbl.abuseat.org"
        ]

    def check_domain_authentication(self, domain):
        if not self.resolver:
            return {"spf": "Skipped", "dkim": "Skipped", "dmarc": "Skipped"}

        results = {}
        try:
            txt_records = self.resolver.resolve(domain, 'TXT')
            spf_record = next((str(r) for r in txt_records if 'v=spf1' in str(r).lower()), None)
            results['spf'] = "✅ Found" if spf_record else "❌ Missing"
        except Exception: results['spf'] = "⚠️ Error"

        try:
            dmarc_domain = f'_dmarc.{domain}'
            txt_records = self.resolver.resolve(dmarc_domain, 'TXT')
            dmarc_record = next((str(r) for r in txt_records if 'v=dmarc1' in str(r).lower()), None)
            results['dmarc'] = "✅ Found" if dmarc_record else "❌ Missing"
        except Exception: results['dmarc'] = "❌ Missing"

        # Note: DKIM is hard to check passively. This is a best-effort guess.
        dkim_found = False
        common_selectors = ["google", "selector1", "selector2", "default", "k1", "dkim"]
        for selector in common_selectors:
            try:
                self.resolver.resolve(f'{selector}._domainkey.{domain}', 'TXT')
                dkim_found = True
                break
            except Exception: continue
        results['dkim'] = "✅ Found (common selector)" if dkim_found else "⚠️ Not Found (common selectors)"
        return results

    def check_blacklist(self, ip_or_domain):
        if not self.resolver: return "Skipped (dnspython not installed)"
        is_ip = re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip_or_domain)

        if is_ip:
            query_target = '.'.join(reversed(ip_or_domain.split('.')))
        else:
            query_target = ip_or_domain

        listed_on = []
        for server in self.blacklist_servers:
            try:
                query = f"{query_target}.{server}"
                self.resolver.resolve(query, 'A')
                listed_on.append(server)
            except dns.resolver.NXDOMAIN:
                continue
            except Exception:
                continue
        return f"Listed on: {', '.join(listed_on)}" if listed_on else "Clean"


    def analyze_spam_ai(self, subject, body_html, provider_type='openai'):
        """Runs AI spam checks."""
        from .ai_handler import AIHandler
        ai_handler = AIHandler() # AIHandler now reads from app config
        
        prompt = (f"Analyze the following email for spam triggers, awkward phrasing, or phishing indicators. "
                  f"Provide a spam score from 1 to 10 (1 is best), a one-sentence summary of the risk, "
                  f"and a bulleted list of concrete suggestions for improvement. Format your response clearly.\n\n"
                  f"SUBJECT: {subject}\n\nBODY:\n{body_html}")
        
        success, result = ai_handler.generate(prompt, system_msg="You are an expert email deliverability analyst.", provider_override=provider_type)
        return success, result


    def spin(self, text):
        """Processes spintax {opt1|opt2} in text."""
        pattern = re.compile(r'{([^{}]*)}')
        while True:
            match = pattern.search(text)
            if not match:
                break
            options = match.group(1).split('|')
            text = text[:match.start()] + random.choice(options) + text[match.end():]
        return text
