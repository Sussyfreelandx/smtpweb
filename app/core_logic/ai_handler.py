import requests
import logging
from flask import current_app

log = logging.getLogger(__name__)

class AIHandler:
    """Handles OpenAI API calls."""

    def __init__(self, api_key, model="gpt-3.5-turbo"):
        self.api_key = api_key
        self.model = model
        self.api_url = "https://api.openai.com/v1/chat/completions"

    def generate(self, prompt, system_msg="You are a helpful email marketing assistant."):
        if not self.api_key:
            return False, "OpenAI API Key is not configured."

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }

        try:
            response = requests.post(self.api_url, headers=headers, json=data, timeout=45)
            response.raise_for_status() # Raise an exception for bad status codes
            
            result = response.json()
            content = result['choices'][0]['message']['content']
            return True, content.strip()
            
        except requests.exceptions.HTTPError as e:
            log.error(f"OpenAI API HTTP Error ({e.response.status_code}): {e.response.text}")
            return False, f"API Error ({e.response.status_code}): {e.response.text}"
        except Exception as e:
            log.error(f"OpenAI Connection Error: {e}")
            return False, f"Connection Error: {e}"

class LocalAIHandler:
    """Handles Local LLM (Ollama) API calls."""

    def __init__(self, api_url, model="llama3"):
        self.api_url = api_url
        self.model = model

    def generate(self, prompt, system_msg="You are a helpful email marketing assistant."):
        if not self.api_url:
            return False, "Local AI URL is not configured."

        headers = {"Content-Type": "application/json"}
        data = {
            "model": self.model,
            "system": system_msg,
            "prompt": prompt,
            "stream": False
        }

        try:
            log.info(f"Calling Local AI at {self.api_url} with model {self.model}...")
            response = requests.post(self.api_url, headers=headers, json=data, timeout=120)
            response.raise_for_status()
            
            result = response.json()
            content = result.get('response', '')
            return True, content.strip()

        except requests.exceptions.HTTPError as e:
            log.error(f"Local AI Error ({e.response.status_code}): {e.response.text}")
            return False, f"Local AI Error ({e.response.status_code}): {e.response.text}"
        except Exception as e:
            log.error(f"Local AI Connection Error: {e}")
            return False, f"Local AI Connection Error: {e}\nIs the local AI server running?"