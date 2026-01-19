import re
import random
import logging

try:
    import dns.resolver
    DNSPYTHON_AVAILABLE = True
except ImportError:
    DNSPYTHON_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError: 
    REQUESTS_AVAILABLE = False

log = logging.getLogger(__name__)


class DeliverabilityHelper:
    def __init__(self):
        if DNSPYTHON_AVAILABLE:
            self.resolver = dns.resolver. Resolver()
            self.resolver.timeout = 3
            self.resolver.lifetime = 3
        else:
            self. resolver = None

        self.blacklist_servers = [
            "bl.spamcop.net", "dnsbl.sorbs.net", "zen.spamhaus. org",
            "b.barracudacentral.org", "cbl.abuseat.org"
        ]

    def check_mx_record(self, domain):
        if not self.resolver:
            return "Skipped"
        try:
            self.resolver.resolve(domain, 'MX')
            return "Valid"
        except Exception:
            return "Invalid"

    def check_domain_authentication(self, domain):
        if not self.resolver:
            return {"spf": "Skipped", "dkim": "Skipped", "dmarc": "Skipped"}

        results = {}

        # Check SPF
        try:
            txt_records = self. resolver.resolve(domain, 'TXT')
            spf_record = next((str(r) for r in txt_records if 'v=spf1' in str(r).lower()), None)
            results['spf'] = "Found" if spf_record else "Missing"
        except Exception:
            results['spf'] = "Error"

        # Check DMARC
        try: 
            dmarc_domain = f'_dmarc. {domain}'
            txt_records = self. resolver.resolve(dmarc_domain, 'TXT')
            dmarc_record = next((str(r) for r in txt_records if 'v=dmarc1' in str(r).lower()), None)
            results['dmarc'] = "Found" if dmarc_record else "Missing"
        except Exception:
            results['dmarc'] = "Missing"

        # Check DKIM (common selectors)
        dkim_found = False
        common_selectors = ["google", "selector1", "selector2", "default", "k1", "dkim"]
        for selector in common_selectors:
            try:
                self.resolver.resolve(f'{selector}._domainkey. {domain}', 'TXT')
                dkim_found = True
                break
            except Exception: 
                continue
        results['dkim'] = "Found (common selector)" if dkim_found else "Not Found"

        return results

    def check_blacklist(self, ip_or_domain):
        if not self.resolver:
            return "Skipped (dnspython not installed)"

        is_ip = re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip_or_domain)

        if is_ip:
            query_target = '. '.join(reversed(ip_or_domain.split('.')))
        else:
            query_target = ip_or_domain

        listed_on = []
        for server in self.blacklist_servers: 
            try:
                query = f"{query_target}.{server}"
                self.resolver.resolve(query, 'A')
                listed_on.append(server)
            except Exception:
                continue

        return f"Listed on:  {', '.join(listed_on)}" if listed_on else "Clean"

    def analyze_spam_ai(self, subject, body_html, provider_type='openai'):
        from app.core_logic.ai_handler import AIHandler
        ai_handler = AIHandler()

        prompt = (f"Analyze the following email for spam triggers, awkward phrasing, or phishing indicators. "
                  f"Provide a spam score from 1 to 10 (1 is best), a one-sentence summary of the risk, "
                  f"and a bulleted list of concrete suggestions for improvement. Format your response clearly.\n\n"
                  f"SUBJECT: {subject}\n\nBODY:\n{body_html}")

        success, result = ai_handler. generate(prompt, system_msg="You are an expert email deliverability analyst.")
        return success, result

    def spin(self, text):
        if not text:
            return text
        pattern = re.compile(r'\{([^{}]*)\}')
        while True: 
            match = pattern.search(text)
            if not match:
                break
            options = match. group(1).split('|')
            text = text[:match.start()] + random.choice(options) + text[match.end():]
        return text
