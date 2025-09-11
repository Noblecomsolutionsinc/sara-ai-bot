# Sara AI Cold Calling Bot 📞🤖

Sara is an AI-powered outbound calling assistant built with:
- **Twilio** (calls & telephony)
- **OpenAI GPT-5** (AI brain)
- **ElevenLabs** (realistic voice)
- **Flask** (server backend)

---

## 🚀 How it works
1. Start a call with `/start_call` → Sara dials the business number.
2. Twilio connects the call and hits `/voice`.
3. Sara runs her dialogue using GPT + ElevenLabs, looping until the call ends.

---

## ⚙️ Deployment (Render.com)
1. Push this repo to GitHub.
2. Create a new Web Service in Render → select this repo.
3. Set environment variables in Render:
   - `OPENAI_API_KEY`
   - `ELEVENLABS_API_KEY`
   - `ELEVENLABS_VOICE_ID`
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_PHONE_NUMBER`
   - `SERVER_URL`
4. Deploy. Render exposes your live `SERVER_URL`.

---

## 🖥 Local development
```bash
pip install -r requirements.txt
python app.py
