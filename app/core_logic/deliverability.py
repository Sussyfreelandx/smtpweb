import re
import random
import logging
import socket
from datetime import datetime, timedelta
from collections import Counter

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
    """Tools for improving email deliverability and effectiveness."""
    
    BLACKLIST_SERVERS = [
        "bl.spamcop.net",
        "dnsbl.sorbs.net",
        "zen.spamhaus.org",
        "b.barracudacentral.org",
        "cbl.abuseat.org",
        "dnsbl-1.uceprotect.net",
        "psbl.surriel.com",
        "db.wpbl.info"
    ]
    
    SPAM_WORDS = [
        "free", "guarantee", "no obligation", "winner", "congratulations",
        "urgent", "act now", "limited time", "exclusive deal", "click here",
        "buy now", "order now", "cash", "credit", "discount", "offer",
        "prize", "bonus", "gift", "million", "billion", "earn money",
        "make money", "work from home", "double your", "increase your",
        "no cost", "no fees", "risk free", "100% free", "apply now",
        "call now", "don't delete", "don't miss", "for free", "get it now",
        "great offer", "incredible deal", "info you requested", "information you requested",
        "instant", "limited offer", "new customer only", "one time", "open immediately",
        "please read", "see for yourself", "special promotion", "this isn't spam",
        "undisclosed recipient", "unsolicited", "urgent response", "you have been selected",
        "your account", "password", "verify", "confirm", "suspended", "unusual activity"
    ]
    
    PHISHING_INDICATORS = [
        r'verify.{0,20}account',
        r'confirm.{0,20}identity',
        r'update.{0,20}payment',
        r'suspend.{0,20}account',
        r'unusual.{0,20}activity',
        r'click.{0,20}immediately',
        r'within.{0,5}\d+.{0,5}hours',
        r'account.{0,20}locked',
        r'security.{0,20}alert',
        r'password.{0,20}expired'
    ]
    
    def __init__(self):
        if DNSPYTHON_AVAILABLE: 
            self.resolver = dns.resolver.Resolver()
            self.resolver.timeout = 5
            self.resolver.lifetime = 5
        else:
            self.resolver = None
    
    def check_mx_record(self, domain):
        """Check if domain has valid MX records."""
        if not self.resolver:
            return "Skipped (dnspython not installed)", []
        
        try: 
            answers = self.resolver.resolve(domain, 'MX')
            mx_records = [str(r.exchange).rstrip('.') for r in answers]
            return "Valid", mx_records
        except dns.resolver.NXDOMAIN:
            return "Invalid (Domain does not exist)", []
        except dns.resolver.NoAnswer:
            return "Invalid (No MX records)", []
        except dns.exception.Timeout:
            return "Timeout", []
        except Exception as e:
            return f"Error: {str(e)[:50]}", []
    
    def check_domain_authentication(self, domain):
        """Check SPF, DKIM, and DMARC records for a domain."""
        if not self.resolver:
            return {
                "spf": "Skipped",
                "dkim": "Skipped",
                "dmarc": "Skipped",
                "details": {}
            }
        
        results = {"details": {}}
        
        # Check SPF
        try:
            txt_records = self.resolver.resolve(domain, 'TXT')
            spf_record = None
            for r in txt_records:
                record_str = str(r).strip('"')
                if record_str.lower().startswith('v=spf1'):
                    spf_record = record_str
                    break
            
            if spf_record:
                results['spf'] = "✅ Found"
                results['details']['spf_record'] = spf_record
                
                # Analyze SPF
                if '-all' in spf_record: 
                    results['details']['spf_policy'] = "Strict (fail)"
                elif '~all' in spf_record: 
                    results['details']['spf_policy'] = "Soft fail"
                elif '?all' in spf_record: 
                    results['details']['spf_policy'] = "Neutral"
                elif '+all' in spf_record: 
                    results['details']['spf_policy'] = "⚠️ Permissive (not recommended)"
            else:
                results['spf'] = "❌ Missing"
        except Exception as e: 
            results['spf'] = f"⚠️ Error: {str(e)[:30]}"
        
        # Check DMARC
        try:
            dmarc_domain = f'_dmarc.{domain}'
            txt_records = self.resolver.resolve(dmarc_domain, 'TXT')
            dmarc_record = None
            for r in txt_records:
                record_str = str(r).strip('"')
                if record_str.lower().startswith('v=dmarc1'):
                    dmarc_record = record_str
                    break
            
            if dmarc_record:
                results['dmarc'] = "✅ Found"
                results['details']['dmarc_record'] = dmarc_record
                
                # Parse DMARC policy
                policy_match = re.search(r'p=(\w+)', dmarc_record)
                if policy_match: 
                    policy = policy_match.group(1).lower()
                    if policy == 'reject':
                        results['details']['dmarc_policy'] = "Reject (strictest)"
                    elif policy == 'quarantine':
                        results['details']['dmarc_policy'] = "Quarantine"
                    elif policy == 'none':
                        results['details']['dmarc_policy'] = "⚠️ None (monitoring only)"
            else:
                results['dmarc'] = "❌ Missing"
        except dns.resolver.NXDOMAIN:
            results['dmarc'] = "❌ Missing"
        except Exception as e: 
            results['dmarc'] = f"⚠️ Error: {str(e)[:30]}"
        
        # Check DKIM (common selectors)
        dkim_found = False
        common_selectors = [
            "google", "selector1", "selector2", "default", "k1", "dkim",
            "mail", "email", "smtp", "s1", "s2", "mx", "key1", "key2"
        ]
        
        found_selector = None
        for selector in common_selectors:
            try:
                dkim_domain = f'{selector}._domainkey.{domain}'
                self.resolver.resolve(dkim_domain, 'TXT')
                dkim_found = True
                found_selector = selector
                break
            except Exception: 
                continue
        
        if dkim_found:
            results['dkim'] = f"✅ Found (selector: {found_selector})"
        else:
            results['dkim'] = "⚠️ Not found (common selectors checked)"
        
        # Calculate overall score
        score = 0
        if "✅" in results.get('spf', ''):
            score += 33
        if "✅" in results.get('dmarc', ''):
            score += 34
        if "✅" in results.get('dkim', ''):
            score += 33
        
        results['overall_score'] = score
        results['recommendation'] = self._get_auth_recommendation(results)
        
        return results
    
    def _get_auth_recommendation(self, results):
        """Get recommendation based on authentication results."""
        missing = []
        
        if "❌" in results.get('spf', ''):
            missing.append("SPF")
        if "❌" in results.get('dmarc', ''):
            missing.append("DMARC")
        if "⚠️" in results.get('dkim', '') or "❌" in results.get('dkim', ''):
            missing.append("DKIM")
        
        if not missing:
            return "✅ All authentication records are properly configured."
        elif len(missing) == 3:
            return "❌ Critical: No email authentication configured. Set up SPF, DKIM, and DMARC immediately."
        else:
            return f"⚠️ Missing: {', '.join(missing)}. Configure these to improve deliverability."
    
    def check_blacklist(self, ip_or_domain):
        """Check if IP or domain is on common blacklists."""
        if not self.resolver:
            return "Skipped (dnspython not installed)", []
        
        # Determine if input is IP or domain
        is_ip = re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip_or_domain)
        
        if is_ip:
            # Reverse the IP for DNSBL lookup
            query_target = '.'.join(reversed(ip_or_domain.split('.')))
        else:
            query_target = ip_or_domain
        
        listed_on = []
        checked = []
        
        for server in self.BLACKLIST_SERVERS:
            try: 
                query = f"{query_target}.{server}"
                self.resolver.resolve(query, 'A')
                listed_on.append(server)
            except dns.resolver.NXDOMAIN:
                # Not listed - this is good
                pass
            except Exception: 
                pass
            checked.append(server)
        
        if listed_on:
            return f"❌ Listed on: {', '.join(listed_on)}", listed_on
        else:
            return f"✅ Clean (checked {len(checked)} blacklists)", []
    
    def check_link_health(self, html_content):
        """Check if links in content are valid and accessible."""
        if not REQUESTS_AVAILABLE:
            return {"error": "Requests library not installed"}
        
        # Extract links
        links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_content, re.IGNORECASE)
        
        if not links:
            return {"message": "No links found in content"}
        
        unique_links = list(set(links))
        results = {}
        
        for link in unique_links[:20]:  # Limit to 20 links
            try:
                response = requests.head(
                    link,
                    timeout=10,
                    allow_redirects=True,
                    headers={'User-Agent': 'Mozilla/5.0 (compatible; LinkChecker/1.0)'}
                )
                
                if 200 <= response.status_code < 300:
                    results[link] = f"✅ OK ({response.status_code})"
                elif 300 <= response.status_code < 400:
                    results[link] = f"↪️ Redirect ({response.status_code})"
                elif response.status_code == 404:
                    results[link] = "❌ Not Found (404)"
                elif response.status_code == 403:
                    results[link] = "⚠️ Forbidden (403)"
                else:
                    results[link] = f"⚠️ Error ({response.status_code})"
            
            except requests.Timeout:
                results[link] = "⚠️ Timeout"
            except requests.ConnectionError:
                results[link] = "❌ Connection Error"
            except Exception as e:
                results[link] = f"⚠️ Error: {str(e)[:30]}"
        
        if len(unique_links) > 20:
            results["_note"] = f"Only first 20 of {len(unique_links)} links checked"
        
        return results
    
    def basic_spam_check(self, subject, body):
        """Perform comprehensive spam word analysis."""
        full_text = f"{subject} {body}".lower()
        
        score = 0
        triggers = []
        details = {}
        
        # Check for spam words
        spam_words_found = []
        for word in self.SPAM_WORDS: 
            if word in full_text:
                spam_words_found.append(word)
                score += 1
        
        if spam_words_found: 
            triggers.append(f"Spam words: {', '.join(spam_words_found[:5])}")
            if len(spam_words_found) > 5:
                triggers[-1] += f" (+{len(spam_words_found) - 5} more)"
            details['spam_words'] = spam_words_found
        
        # Check for phishing patterns
        phishing_found = []
        for pattern in self.PHISHING_INDICATORS: 
            if re.search(pattern, full_text, re.IGNORECASE):
                phishing_found.append(pattern)
                score += 2
        
        if phishing_found:
            triggers.append("Phishing-like patterns detected")
            details['phishing_patterns'] = len(phishing_found)
        
        # Check subject line issues
        if subject: 
            if subject.isupper():
                score += 2
                triggers.append("ALL CAPS subject line")
            
            if subject.count('!') >= 2:
                score += 1
                triggers.append("Multiple exclamation marks in subject")
            
            if subject.count('?') >= 2:
                score += 1
                triggers.append("Multiple question marks in subject")
            
            if re.search(r'\$+|\$\d', subject):
                score += 1
                triggers.append("Dollar signs in subject")
            
            if len(subject) > 100:
                score += 1
                triggers.append("Subject line too long (>100 chars)")
        
        # Check body issues
        if body:
            # Excessive capitalization
            upper_count = sum(1 for c in body if c.isupper())
            lower_count = sum(1 for c in body if c.islower())
            if lower_count > 0 and upper_count / lower_count > 0.3:
                score += 1
                triggers.append("Excessive capitalization in body")
            
            # Excessive exclamation
            if body.count('!') > 5:
                score += 1
                triggers.append("Too many exclamation marks")
            
            # URL shorteners
            shorteners = ['bit.ly', 'tinyurl', 'goo.gl', 't.co', 'ow.ly', 'is.gd']
            for shortener in shorteners:
                if shortener in body.lower():
                    score += 1
                    triggers.append(f"URL shortener ({shortener})")
                    break
            
            # Check for image-only content
            img_count = len(re.findall(r'<img', body, re.IGNORECASE))
            text_content = re.sub(r'<[^>]+>', '', body).strip()
            if img_count > 0 and len(text_content) < 100:
                score += 2
                triggers.append("Low text-to-image ratio")
            
            # Check for hidden text
            if re.search(r'font-size:\s*0|display:\s*none|visibility:\s*hidden', body, re.IGNORECASE):
                score += 3
                triggers.append("Hidden text detected")
        
        # Determine rating
        if score <= 2:
            rating = "Low Risk"
            color = "success"
        elif score <= 5:
            rating = "Medium Risk"
            color = "warning"
        else:
            rating = "High Risk"
            color = "danger"
        
        return {
            "score": min(score, 10),
            "max_score": 10,
            "triggers": triggers,
            "rating": rating,
            "color": color,
            "details": details,
            "recommendation": self._get_spam_recommendation(score, triggers)
        }
    
    def _get_spam_recommendation(self, score, triggers):
        """Get recommendations based on spam check results."""
        if score <= 2:
            return "Your email looks good! Minor improvements may still help deliverability."
        elif score <= 5:
            recs = ["Consider the following improvements:"]
            if any("spam word" in t.lower() for t in triggers):
                recs.append("- Replace spam trigger words with alternatives")
            if any("caps" in t.lower() for t in triggers):
                recs.append("- Use proper capitalization")
            if any("exclamation" in t.lower() for t in triggers):
                recs.append("- Reduce exclamation marks")
            return "\n".join(recs)
        else:
            return "⚠️ High spam risk! Significantly revise your content before sending. Consider using our AI rewrite feature."
    
    def analyze_spam_ai(self, subject, body_html, provider_type='openai'):
        """Analyze content for spam triggers using AI."""
        from app.core_logic.ai_handler import AIHandler
        
        ai_handler = AIHandler(provider=provider_type)
        
        prompt = f"""Analyze the following email for spam triggers, deliverability issues, and potential problems. 

SUBJECT: {subject}

BODY: 
{body_html[:4000]}

Provide a comprehensive analysis including:
1. Spam Score (1-10, where 1 is safest and 10 is highest risk)
2. Risk Level (Low/Medium/High)
3. Specific spam triggers found
4. Phishing or suspicious indicators
5. Deliverability concerns
6. Concrete suggestions for improvement
7. Estimated inbox placement rate

Format your response with clear sections."""

        system_msg = "You are an expert email deliverability analyst with deep knowledge of spam filters, email authentication, and inbox placement strategies."
        
        return ai_handler.generate(prompt, system_msg)
    
    def check_sender_reputation(self, domain):
        """Check various reputation indicators for a sending domain."""
        results = {
            "domain": domain,
            "checks": {},
            "overall_status": "Unknown"
        }
        
        # Check MX records
        mx_status, mx_records = self.check_mx_record(domain)
        results["checks"]["mx_records"] = {
            "status": mx_status,
            "records": mx_records[:3] if mx_records else []
        }
        
        # Check authentication
        auth = self.check_domain_authentication(domain)
        results["checks"]["authentication"] = {
            "spf": auth.get("spf"),
            "dkim": auth.get("dkim"),
            "dmarc": auth.get("dmarc"),
            "score": auth.get("overall_score", 0)
        }
        
        # Check blacklists
        bl_status, bl_listed = self.check_blacklist(domain)
        results["checks"]["blacklist"] = {
            "status": bl_status,
            "listed_on": bl_listed
        }
        
        # Calculate overall status
        issues = 0
        if "Invalid" in mx_status or "Error" in mx_status:
            issues += 2
        if auth.get("overall_score", 0) < 66:
            issues += 1
        if bl_listed:
            issues += 3
        
        if issues == 0:
            results["overall_status"] = "✅ Good"
            results["overall_message"] = "Your sending domain has good reputation indicators."
        elif issues <= 2:
            results["overall_status"] = "⚠️ Fair"
            results["overall_message"] = "Some issues detected. Review authentication settings."
        else:
            results["overall_status"] = "❌ Poor"
            results["overall_message"] = "Significant issues found. Address these before sending campaigns."
        
        return results
    
    def spin(self, text):
        """Process spintax {option1|option2|option3} in text."""
        if not text:
            return text
        
        pattern = re.compile(r'\{([^{}]*)\}')
        
        while True:
            match = pattern.search(text)
            if not match:
                break
            
            options = match.group(1).split('|')
            replacement = random.choice(options)
            text = text[:match.start()] + replacement + text[match.end():]
        
        return text
    
    def calculate_inbox_score(self, subject, body, sender_domain=None):
        """Calculate an estimated inbox placement score."""
        score = 100
        factors = []
        
        # Spam check
        spam_result = self.basic_spam_check(subject, body)
        spam_penalty = spam_result["score"] * 5
        score -= spam_penalty
        if spam_penalty > 0:
            factors.append(f"Spam triggers: -{spam_penalty}")
        
        # Authentication check (if domain provided)
        if sender_domain and self.resolver:
            auth = self.check_domain_authentication(sender_domain)
            auth_score = auth.get("overall_score", 0)
            auth_penalty = (100 - auth_score) // 4
            score -= auth_penalty
            if auth_penalty > 0:
                factors.append(f"Authentication: -{auth_penalty}")
        
        # Content quality
        if body:
            # Text-to-HTML ratio
            text_content = re.sub(r'<[^>]+>', '', body).strip()
            if len(text_content) < 50:
                score -= 10
                factors.append("Low text content: -10")
            
            # Unsubscribe link
            if 'unsubscribe' not in body.lower():
                score -= 15
                factors.append("No unsubscribe link: -15")
        
        # Subject line quality
        if subject: 
            if len(subject) < 10:
                score -= 5
                factors.append("Short subject: -5")
            elif len(subject) > 80:
                score -= 5
                factors.append("Long subject: -5")
        
        score = max(0, min(100, score))
        
        if score >= 80:
            rating = "Excellent"
            color = "success"
        elif score >= 60:
            rating = "Good"
            color = "info"
        elif score >= 40:
            rating = "Fair"
            color = "warning"
        else:
            rating = "Poor"
            color = "danger"
        
        return {
            "score": score,
            "rating": rating,
            "color": color,
            "factors": factors,
            "recommendation": f"Estimated {score}% inbox placement rate"
        }
