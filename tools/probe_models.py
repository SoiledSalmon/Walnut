"""Read-only API capability smoke test; never print credentials."""
import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

root=Path(__file__).resolve().parents[2]
load_dotenv(root/'code/.env');load_dotenv(root/'.env')
client=genai.Client(api_key=os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY'),http_options=types.HttpOptions(timeout=30000))
for model in ('gemini-3.1-flash-lite','gemini-3.5-flash-lite'):
    try:
        response=client.models.generate_content(model=model,contents='Reply with OK only.',config=types.GenerateContentConfig(max_output_tokens=30,automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        print(model,'plain:',response.text)
    except Exception as error:
        print(model,type(error).__name__,getattr(error,'code',None),str(error)[:350])
