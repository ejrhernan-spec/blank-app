from google import genai
import streamlit as st

key = st.secrets.get("GEMINI_API_KEY", "")
client = genai.Client(api_key=key)

print("--- QUERYING ACCESSIBLE MODELS ---")
candidate_models = [
    "gemini-2.5-flash",
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-flash-latest",
]

for model_name in candidate_models:
  try:
    print(f"Testing {model_name}...")
    res = client.models.generate_content(model=model_name, contents="ping")
    print(f"✅ SUCCESS on {model_name}! Response: {res.text.strip()}\n")
    break
  except Exception as e:
    err_msg = str(e)
    if "404" in err_msg:
      print(f"❌ {model_name}: 404 (Not available to account)")
    elif "503" in err_msg:
      print(f"⚠️ {model_name}: 503 (Cluster busy)")
    else:
      print(f"❌ {model_name}: {err_msg[:80]}...")
      