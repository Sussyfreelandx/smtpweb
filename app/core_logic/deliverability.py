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
            self.resolver = None

        self.blacklist_servers = [
            "bl.spamcop.net", "dnsbl.sorbs.net", "zen.spamhaus. org",
            "b.barracudacentral.org", "cbl.abuseat.org"
        ]

    def check_mx_record(self, domain):
        """Check if domain has valid MX records."""
        if not self.resolver:
            return "Skipped"
        try:
            self.resolver.resolve(domain, 'MX')
            return "Valid"
        except dns.resolver.NXDOMAIN: 
            return "Invalid (No Domain)"
        except dns.resolver.NoAnswer:
            return "Invalid (No MX)"
        except dns.exception.Timeout:
            return "Timeout"
        except Exception:
            return "Error"

    def check_domain_authentication(self, domain):
        """Check SPF, DKIM, and DMARC records."""
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
        """Check if IP or domain is blacklisted."""
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

    def check_link_health(self, html_content):
        """Check if links in content are valid."""
        if not REQUESTS_AVAILABLE: 
            return {"error": "Requests library not available"}

        links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_content)
        if not links:
            return {"message": "No links found"}

        results = {}
        for link in set(links):
            try:
                response = requests.head(link, timeout=5, allow_redirects=True)
                if 200 <= response. status_code < 400:
                    results[link] = f"OK ({response.status_code})"
                else:
                    results[link] = f"Bad ({response.status_code})"
            except requests.exceptions.RequestException as e:
                results[link] = f"Error: {str(e)[:50]}"
        return results

    def analyze_spam_ai(self, subject, body_html, provider_type='openai'):
        """Analyze content for spam triggers using AI."""
        from app.core_logic.ai_handler import AIHandler
        ai_handler = AIHandler()

        prompt = (f"Analyze the following email for spam triggers, awkward phrasing, or phishing indicators. "
                  f"Provide a spam score from 1 to 10 (1 is best), a one-sentence summary of the risk, "
                  f"and a bulleted list of concrete suggestions for improvement. Format your response clearly.\n\n"
                  f"SUBJECT: {subject}\n\nBODY:\n{body_html}")

        success, result = ai_handler.generate(prompt, system_msg="You are an expert email deliverability analyst.")
        return success, result

    def basic_spam_check(self, subject, body):
        """Perform basic spam word analysis."""
        spam_words = [
            "free", "guarantee", "credit", "offer", "urgent", "winner", 
            "cash", "bonus", "buy now", "limited time", "act now",
            "click here", "congratulations", "prize", "million"
        ]
        
        score = 0
        triggers = []
        full_text = (subject + " " + body).lower()

        for word in spam_words:
            if word in full_text:
                score += 1
                triggers.append(word)

        if subject and subject.isupper():
            score += 2
            triggers.append("ALL CAPS SUBJECT")

        if "!" * 3 in full_text:
            score += 1
            triggers.append("Excessive Exclamation")

        if "$$" in full_text or "$$$" in full_text:
            score += 1
            triggers.append("Multiple Dollar Signs")

        return {
            "score":  min(score, 10),
            "triggers": triggers,
            "rating": "Low Risk" if score < 3 else "Medium Risk" if score < 6 else "High Risk"
        }

    def spin(self, text):
        """Process spintax {option1|option2}."""
        if not text:
            return text
        pattern = re.compile(r'\{([^{}]*)\}')
        while True:
            match = pattern.search(text)
            if not match: 
                break
            options = match.group(1).split('|')
            text = text[:match.start()] + random.choice(options) + text[match.end():]
        return text
