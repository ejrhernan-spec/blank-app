from google import genai
import streamlit as st

key = st.secrets.get("GEMINI_API_KEY", "")

print("--- DIAGNOSTIC RESULTS ---")
print(f"Key Found: {'Yes' if key else 'No'}")
print(f"Character Count: {len(key)}")
print(f"Starts with 'AIza': {key.startswith('AIza')}")
if key:
  print(f"First 6 characters: {key[:6]}")
  print(f"Last 4 characters: {key[-4:]}")
print("--------------------------")

# Test live API call
try:
  client = genai.Client(api_key=key)
  response = client.models.generate_content(
      model="gemini-2.5-flash", contents="Hello"
  )
  print("\n CONNECTION SUCCESSFUL! Google accepted the key.")
except Exception as e:
  print(f"\n GOOGLE REJECTED KEY: {e}")
  