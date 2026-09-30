// Fallback defaults, used only when ui/ is hosted as plain static files.
// serve.py replaces this with values from AGENT_URL / AGENT_API_KEY in the environment
// or ui/.env. For static hosting, regenerate it with: python serve.py --write-config
window.PROPERTY_FINDER_CONFIG = {
  "agentUrl": "http://localhost:8000",
  "apiKey": ""
};
