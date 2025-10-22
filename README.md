# Gemini Live Audio Test Application

Real-time voice conversation application using Google's Gemini 2.5 Flash Native Audio API.

## Prerequisites

- Python 3.11+
- Google Gemini API key
- Modern web browser (Chrome/Edge recommended)

## Installation

1. **Clone the repository**

   ```bash
   git clone <your-repo-url>
   cd audio_sample
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**

   Create a `.env` file in the project root:

   ```
   GEMINI_API_KEY=your_api_key_here
   ```

   Get your API key from [Google AI Studio](https://aistudio.google.com/apikey)

## Usage

1. **Start the server**

   ```bash
   uvicorn main:app --reload
   ```

   Server will run at `http://127.0.0.1:8000`

2. **Open the web interface**

   Open `exotel_client.html` in your browser (Chrome/Edge recommended)

3. **Start conversation**
   - Click "Connect" to connect to the server
   - Click "Start Conversation" to begin
   - Speak naturally - Gemini uses automatic turn detection
   - Listen to AI responses through your speakers

## How It Works

```
Browser (Mic) → WebSocket → FastAPI Server → Gemini Live API
                                                     ↓
Browser (Speaker) ← WebSocket ← FastAPI Server ← Audio Response
```

**Audio Pipeline:**

- Client: 48kHz → 8kHz PCM16-LE
- Server: 8kHz → 16kHz (to Gemini)
- Gemini: 24kHz → 8kHz (to client)

## Configuration

Edit `main.py` to customize:

```python
# Change AI persona
system_instruction = "You are..."

# Change voice
voice_name = "Zephyr"  # or "Kore", "Puck", "Charon", "Aoede"

# Change language
language_code = "en-US"  # or "hi-IN", "es-ES", etc.
```

## Troubleshooting

### No audio playback

- Check browser microphone permissions
- Ensure speakers/headphones are connected
- Check browser console (F12) for errors
- Use headphones to prevent echo

### Connection issues

- Verify server is running at `http://127.0.0.1:8000`
- Check `GEMINI_API_KEY` is valid
- Ensure port 8000 is not in use

### Audio quality issues

- Use headphones to prevent feedback
- Speak clearly and at moderate pace
- Check microphone quality

## Project Structure

```
audio_sample/
├── main.py                 # FastAPI server (Gemini Live bridge)
├── exotel_client.html      # Web interface
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (create this)
└── README.md              # This file
```

## Technologies

- **Backend:** FastAPI, WebSocket
- **AI:** Google Gemini 2.5 Flash Native Audio
- **Frontend:** Vanilla JavaScript, Web Audio API
- **Audio:** PCM16 format, real-time resampling
