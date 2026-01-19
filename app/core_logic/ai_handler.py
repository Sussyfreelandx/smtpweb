import logging
from flask import current_app

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError: 
    REQUESTS_AVAILABLE = False

log = logging.getLogger(__name__)


class AIHandler:
    def __init__(self):
        pass

    def generate(self, prompt, system_msg="You are a helpful email marketing assistant. "):
        api_key = current_app.config. get('OPENAI_API_KEY')
        local_url = current_app.config.get('LOCAL_AI_URL')

        if local_url:
            return self._generate_local(prompt, system_msg, local_url)
        elif api_key: 
            return self._generate_openai(prompt, system_msg, api_key)
        else: 
            return False, "No AI provider is configured.  Please set OPENAI_API_KEY or LOCAL_AI_URL."

    def _generate_openai(self, prompt, system_msg, api_key):
        if not REQUESTS_AVAILABLE: 
            return False, "Requests library missing."

        api_url = "https://api.openai.com/v1/chat/completions"
        model = "gpt-3.5-turbo"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }

        try: 
            response = requests. post(api_url, headers=headers, json=data, timeout=45)
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                return True, content. strip()
            else:
                log.error(f"OpenAI API Error ({response.status_code}): {response.text}")
                return False, f"API Error ({response.status_code}): {response.text}"
        except Exception as e:
            log.error(f"OpenAI Connection Error: {e}")
            return False, f"Connection Error: {e}"

    def _generate_local(self, prompt, system_msg, api_url):
        if not REQUESTS_AVAILABLE:
            return False, "Requests library missing."

        model = "llama3"

        headers = {"Content-Type": "application/json"}
        data = {
            "model": model,
            "system": system_msg,
            "prompt": prompt,
            "stream": False
        }

        try: 
            log.info(f"Calling Local AI at {api_url}...")
            response = requests.post(api_url, headers=headers, json=data, timeout=120)
            if response.status_code == 200:
                result = response.json()
                content = result. get('response', '')
                return True, content. strip()
            else:
                log. error(f"Local AI Error ({response.status_code}): {response.text}")
                return False, f"Local AI Error ({response. status_code}): {response.text}"
        except Exception as e:
            log.error(f"Local AI Connection Error:  {e}")
            return False, f"Local AI Connection Error: {e}"
