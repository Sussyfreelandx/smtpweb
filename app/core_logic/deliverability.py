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

    def analyze_spam(self, subject, body_html):
        """Runs both basic and AI spam checks."""
        report = {
            'basic_score': 0,
            'basic_triggers': [],
            'ai_analysis': "AI analysis skipped or failed.",
        }

        # Basic Check
        spam_words = ["free", "guarantee", "credit", "offer", "urgent", "winner", "cash", "bonus", "buy now"]
        full_text = (subject + " " + body_html).lower()
        for word in spam_words:
            if re.search(fr'\b{word}\b', full_text):
                report['basic_score'] += 1
                report['basic_triggers'].append(word)
        if subject.isupper() and len(subject) > 10:
            report['basic_score'] += 2
            report['basic_triggers'].append("ALL CAPS SUBJECT")
        if "!" * 3 in full_text:
            report['basic_score'] += 1
            report['basic_triggers'].append("Excessive Exclamation")
        
        # AI Check
        from .ai_handler import AIHandler
        ai_handler = AIHandler()
        prompt = (f"Analyze the following email for spam triggers, awkward phrasing, or phishing indicators. "
                  f"Provide a spam score from 1 to 10 (1 is best), a one-sentence summary of the risk, "
                  f"and a bulleted list of concrete suggestions for improvement. Format your response clearly.\n\n"
                  f"SUBJECT: {subject}\n\nBODY:\n{body_html}")
        success, result = ai_handler.generate(prompt, system_msg="You are an expert email deliverability analyst.")
        if success:
            report['ai_analysis'] = result
            
        return report

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
