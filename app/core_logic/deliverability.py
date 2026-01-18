import re
import dns.resolver
import requests
import random

class DeliverabilityHelper:
    """Tools for improving deliverability and effectiveness, adapted for web use."""
    def __init__(self):
        self.resolver = dns.resolver.Resolver()
        self.resolver.timeout = 3
        self.resolver.lifetime = 3
        self.blacklist_servers = [
            "bl.spamcop.net", "dnsbl.sorbs.net", "zen.spamhaus.org",
            "b.barracudacentral.org", "cbl.abuseat.org"
        ]

    def check_mx_record(self, domain):
        try:
            self.resolver.resolve(domain, 'MX')
            return "Valid"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return "Invalid"
        except dns.exception.Timeout:
            return "Timeout"
        except Exception:
            return "Error"

    def check_domain_authentication(self, domain):
        results = {}
        try:
            txt_records = self.resolver.resolve(domain, 'TXT')
            spf_record = next((str(r) for r in txt_records if 'v=spf1' in str(r).lower()), None)
            results['spf'] = "✅ Found" if spf_record else "❌ Missing"
        except Exception:
            results['spf'] = "⚠️ Error"

        try:
            dmarc_domain = f'_dmarc.{domain}'
            txt_records = self.resolver.resolve(dmarc_domain, 'TXT')
            dmarc_record = next((str(r) for r in txt_records if 'v=dmarc1' in str(r).lower()), None)
            results['dmarc'] = "✅ Found" if dmarc_record else "❌ Missing"
        except Exception:
            results['dmarc'] = "❌ Missing"

        dkim_found = False
        common_selectors = ["google", "selector1", "selector2", "default", "k1", "dkim"]
        for selector in common_selectors:
            try:
                self.resolver.resolve(f'{selector}._domainkey.{domain}', 'TXT')
                dkim_found = True
                break
            except Exception:
                continue
        results['dkim'] = "✅ Found (common)" if dkim_found else "⚠️ Not Found (common)"
        return results

    def check_blacklist(self, ip_or_domain):
        is_ip = re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip_or_domain)
        query_target = '.'.join(reversed(ip_or_domain.split('.'))) if is_ip else ip_or_domain

        listed_on = []
        for server in self.blacklist_servers:
            try:
                self.resolver.resolve(f"{query_target}.{server}", 'A')
                listed_on.append(server)
            except dns.resolver.NXDOMAIN:
                continue
            except Exception:
                continue
        return f"Listed on: {', '.join(listed_on)}" if listed_on else "Clean"

    def spin(self, text):
        pattern = re.compile(r'{([^{}]*)}')
        while True:
            match = pattern.search(text)
            if not match:
                break
            options = match.group(1).split('|')
            text = text[:match.start()] + random.choice(options) + text[match.end():]
        return text