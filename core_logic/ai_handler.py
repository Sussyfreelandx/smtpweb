import requests
import logging

log = logging.getLogger(__name__)

class AIHandler:
    """Handles OpenAI API calls."""

    def __init__(self, api_key, model="gpt-3.5-turbo"):
        self.api_key = api_key
        self.model = model
        self.api_url = "https://api.openai.com/v1/chat/completions"

    def generate(self, prompt, system_msg="You are a helpful email marketing assistant."):
        if not self.api_key:
            return False, "API Key missing. Please configure in Settings."

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
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                return True, content.strip()
            else:
                log.error(f"OpenAI API Error ({response.status_code}): {response.text}")
                return False, f"API Error ({response.status_code})"
        except Exception as e:
            log.error(f"OpenAI Connection Error: {e}")
            return False, f"Connection Error: {e}"

class LocalAIHandler:
    """Handles Local LLM (Ollama) API calls."""

    def __init__(self, api_url, model="llama3"):
        self.api_url = api_url if api_url else "http://localhost:11434/api/generate"
        self.model = model if model else "llama3"

    def generate(self, prompt, system_msg="You are a helpful email marketing assistant."):
        if not self.api_url:
            return False, "Local AI URL missing. Please configure in Settings."

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
            if response.status_code == 200:
                result = response.json()
                content = result.get('response', '')
                return True, content.strip()
            else:
                log.error(f"Local AI Error ({response.status_code}): {response.text}")
                return False, f"Local AI Error ({response.status_code})"
        except Exception as e:
            log.error(f"Local AI Connection Error: {e}")
            return False, f"Local AI Connection Error: {e}"