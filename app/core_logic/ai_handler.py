import requests
import logging
from flask import current_app

log = logging.getLogger(__name__)

class AIHandler:
    """Handles OpenAI and Local LLM API calls, adapted for Flask."""

    def __init__(self):
        # Configuration will be pulled from the Flask app context
        pass

    def _get_config(self, provider_override=None):
        """Determines which AI provider to use based on configuration."""
        if provider_override:
            if provider_override == 'local' and current_app.config.get('LOCAL_AI_URL'):
                return 'local'
            if provider_override == 'openai' and current_app.config.get('OPENAI_API_KEY'):
                return 'openai'
        
        # Default behavior: prioritize local AI if configured
        if current_app.config.get('LOCAL_AI_URL'):
            return 'local'
        if current_app.config.get('OPENAI_API_KEY'):
            return 'openai'
            
        return None

    def generate(self, prompt, system_msg="You are a helpful email marketing assistant.", provider_override=None):
        provider = self._get_config(provider_override)

        if provider == "openai":
            return self._generate_openai(prompt, system_msg)
        elif provider == "local":
            return self._generate_local(prompt, system_msg)
        else:
            msg = "No AI provider is configured. Please set OPENAI_API_KEY or LOCAL_AI_URL in your .env file."
            log.warning(msg)
            return False, msg

    def _generate_openai(self, prompt, system_msg):
        api_key = current_app.config.get('OPENAI_API_KEY')
        api_url = "https://api.openai.com/v1/chat/completions"
        model = current_app.config.get('OPENAI_MODEL', 'gpt-3.5-turbo')

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}],
            "temperature": 0.7
        }

        try:
            response = requests.post(api_url, headers=headers, json=data, timeout=45)
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                return True, content.strip()
            else:
                log.error(f"OpenAI API Error ({response.status_code}): {response.text}")
                return False, f"API Error ({response.status_code}): {response.text}"
        except Exception as e:
            log.error(f"OpenAI Connection Error: {e}")
            return False, f"Connection Error: {e}"

    def _generate_local(self, prompt, system_msg):
        api_url = current_app.config.get('LOCAL_AI_URL')
        model = current_app.config.get('LOCAL_AI_MODEL', 'llama3')

        headers = {"Content-Type": "application/json"}
        data = {
            "model": model,
            "system": system_msg,
            "prompt": prompt,
            "stream": False
        }

        try:
            log.info(f"Calling Local AI at {api_url} with model {model}...")
            response = requests.post(api_url, headers=headers, json=data, timeout=120)
            if response.status_code == 200:
                result = response.json()
                content = result.get('response', '')
                return True, content.strip()
            else:
                log.error(f"Local AI Error ({response.status_code}): {response.text}")
                return False, f"Local AI Error ({response.status_code}): {response.text}"
        except Exception as e:
            log.error(f"Local AI Connection Error: {e}")
            return False, f"Local AI Connection Error: {e}\nIs the local AI service running?"
