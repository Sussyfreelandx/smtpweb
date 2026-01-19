import logging
import re
import json
from flask import current_app

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

log = logging.getLogger(__name__)


class AIHandler:
    """Handles AI operations using OpenAI or local LLM."""
    
    def __init__(self, provider='openai', api_key=None, local_url=None, model=None):
        self.provider = provider
        self.api_key = api_key
        self.local_url = local_url or "http://localhost:11434/api/generate"
        self.model = model
        
        if not self.api_key:
            try:
                self.api_key = current_app.config.get('OPENAI_API_KEY')
            except RuntimeError:
                pass
        
        if not self.local_url or self.local_url == "http://localhost:11434/api/generate":
            try:
                self.local_url = current_app.config.get('LOCAL_AI_URL', self.local_url)
            except RuntimeError:
                pass
    
    def generate(self, prompt, system_msg="You are a helpful email marketing assistant.", max_tokens=2000):
        """Generate content using configured AI provider."""
        if self.provider == 'local' and self.local_url:
            return self._generate_local(prompt, system_msg, max_tokens)
        elif self.api_key:
            return self._generate_openai(prompt, system_msg, max_tokens)
        else:
            return False, "No AI provider configured. Please set OPENAI_API_KEY or LOCAL_AI_URL."
    
    def _generate_openai(self, prompt, system_msg, max_tokens):
        """Generate content using OpenAI API."""
        if not REQUESTS_AVAILABLE:
            return False, "Requests library not installed."
        
        api_url = "https://api.openai.com/v1/chat/completions"
        model = self.model or "gpt-3.5-turbo"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": max_tokens
        }
        
        try: 
            response = requests.post(api_url, headers=headers, json=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                return True, content.strip()
            elif response.status_code == 401:
                return False, "Invalid API key. Please check your OpenAI API key."
            elif response.status_code == 429:
                return False, "Rate limit exceeded. Please try again later."
            else:
                log.error(f"OpenAI API Error ({response.status_code}): {response.text}")
                return False, f"API Error ({response.status_code}): {response.text[:200]}"
        
        except requests.Timeout:
            return False, "Request timed out. Please try again."
        except requests.RequestException as e:
            log.error(f"OpenAI Connection Error: {e}")
            return False, f"Connection Error: {str(e)}"
        except Exception as e:
            log.error(f"OpenAI Error: {e}")
            return False, f"Error: {str(e)}"
    
    def _generate_local(self, prompt, system_msg, max_tokens):
        """Generate content using local LLM (Ollama)."""
        if not REQUESTS_AVAILABLE:
            return False, "Requests library not installed."
        
        model = self.model or "llama3"
        
        headers = {"Content-Type": "application/json"}
        
        data = {
            "model": model,
            "system": system_msg,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens
            }
        }
        
        try:
            log.info(f"Calling Local AI at {self.local_url}...")
            response = requests.post(self.local_url, headers=headers, json=data, timeout=120)
            
            if response.status_code == 200:
                result = response.json()
                content = result.get('response', '')
                return True, content.strip()
            else:
                log.error(f"Local AI Error ({response.status_code}): {response.text}")
                return False, f"Local AI Error ({response.status_code}): {response.text[:200]}"
        
        except requests.Timeout:
            return False, "Local AI request timed out. Check if Ollama is running."
        except requests.ConnectionError:
            return False, "Could not connect to local AI. Is Ollama running?"
        except Exception as e:
            log.error(f"Local AI Error: {e}")
            return False, f"Local AI Error: {str(e)}"
    
    def rewrite_content(self, content, style="professional", preserve_html=True):
        """Rewrite email content to be more engaging."""
        html_note = "Preserve HTML structure and any placeholders like {{variable}}." if preserve_html else ""
        
        prompt = f"""Rewrite the following email content to be more {style} and engaging. 
{html_note}
Keep the core message but improve clarity, persuasiveness, and readability. 

Original content:
{content}

Rewritten content:"""
        
        return self.generate(prompt)
    
    def generate_subjects(self, content, count=5, style="professional"):
        """Generate subject line suggestions based on email content."""
        prompt = f"""Based on the following email content, generate {count} compelling subject lines. 

Requirements:
- Each subject line should be under 60 characters
- Make them attention-grabbing but not spammy
- Vary the approach (question, benefit, curiosity, urgency, personalization)
- Style: {style}

Email content:
{content[:2000]}

Generate exactly {count} subject lines, one per line, numbered 1-{count}: """
        
        return self.generate(prompt)
    
    def analyze_for_spam(self, subject, body):
        """Analyze email content for spam triggers."""
        prompt = f"""Analyze the following email for spam triggers and deliverability issues.

Subject: {subject}

Body:
{body[:3000]}

Provide: 
1. Overall spam score (1-10, where 1 is best/safe and 10 is highest spam risk)
2. Risk level (Low/Medium/High)
3. List of specific spam triggers found
4. Specific recommendations for improvement
5. Estimated deliverability rating (Excellent/Good/Fair/Poor)

Format your response clearly with headers for each section."""
        
        system_msg = "You are an expert email deliverability analyst with deep knowledge of spam filters, email best practices, and inbox placement strategies."
        
        return self.generate(prompt, system_msg)
    
    def generate_email_variations(self, content, count=3, target_audience="general"):
        """Generate variations of email content for A/B testing."""
        prompt = f"""Create {count} variations of the following email for A/B testing.
Target audience: {target_audience}

Original email:
{content[:2000]}

For each variation: 
1. Maintain the core message and call-to-action
2. Vary the tone, structure, or approach
3. Preserve any HTML structure and placeholders

Generate {count} complete email variations, clearly separated: """
        
        return self.generate(prompt)
    
    def suggest_send_time(self, audience_info="general business"):
        """Suggest optimal send times based on audience."""
        prompt = f"""Based on email marketing best practices and the target audience described below,
suggest the optimal send times for an email campaign.

Target audience: {audience_info}

Provide:
1. Best days of the week to send
2. Optimal time ranges (in major timezones: EST, PST, GMT)
3. Times to avoid
4. Reasoning for your recommendations"""
        
        return self.generate(prompt)
    
    def improve_call_to_action(self, current_cta, context=""):
        """Improve a call-to-action."""
        prompt = f"""Improve the following call-to-action (CTA) for an email: 

Current CTA: {current_cta}
Context: {context if context else 'General marketing email'}

Provide 5 improved CTA variations that are:
1. Action-oriented
2. Create urgency or value
3. Clear and specific
4. Appropriate for email (not too long)

List each improved CTA on a new line:"""
        
        return self.generate(prompt)
    
    def generate_preheader(self, subject, body_preview=""):
        """Generate email preheader text."""
        prompt = f"""Generate an effective email preheader text that complements this subject line. 

Subject: {subject}
Email preview: {body_preview[:500] if body_preview else 'Not provided'}

Requirements:
- 40-100 characters ideal
- Should complement, not repeat, the subject
- Create curiosity or add value
- Encourage the open

Provide 3 preheader options, one per line:"""
        
        return self.generate(prompt)
    
    def personalize_for_segment(self, base_content, segment_description):
        """Personalize content for a specific segment."""
        prompt = f"""Adapt the following email content for this specific audience segment: 

Segment: {segment_description}

Original content:
{base_content[:2000]}

Requirements:
- Maintain the core message and CTA
- Adjust tone and language for the segment
- Highlight benefits relevant to this audience
- Keep all placeholders and HTML structure

Adapted content:"""
        
        return self.generate(prompt)
    
    def extract_insights_from_metrics(self, metrics_data):
        """Analyze campaign metrics and provide insights."""
        prompt = f"""Analyze the following email campaign metrics and provide actionable insights: 

Metrics: 
{json.dumps(metrics_data, indent=2) if isinstance(metrics_data, dict) else metrics_data}

Provide: 
1. Key observations about performance
2. Comparison to industry benchmarks (if applicable)
3. Specific recommendations for improvement
4. What to test in the next campaign
5. Any warning signs or issues to address"""
        
        system_msg = "You are an email marketing analytics expert who provides data-driven insights and recommendations."
        
        return self.generate(prompt, system_msg)
    
    def clean_email_list_suggestions(self, bounce_rate, unsubscribe_rate, complaint_rate):
        """Provide suggestions for cleaning and maintaining email list."""
        prompt = f"""Based on the following email list health metrics, provide recommendations: 

Bounce Rate: {bounce_rate}%
Unsubscribe Rate: {unsubscribe_rate}%
Complaint Rate: {complaint_rate}%

Provide:
1. Assessment of current list health
2. Recommended actions based on these metrics
3. Best practices for list hygiene
4. Re-engagement strategies if needed
5. Prevention strategies for future issues"""
        
        return self.generate(prompt)
