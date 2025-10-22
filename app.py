import os
import json
import base64
import asyncio
import logging
import audioop
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from urllib.parse import parse_qs

from google import genai
from google.genai import types

# ==================================================
# ENV + LOGGING
# ==================================================
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = FastAPI()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "models/gemini-2.5-flash-native-audio-preview-09-2025"

client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY,
)

# ==================================================
# GEMINI CONFIG
# ==================================================
CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    media_resolution="MEDIA_RESOLUTION_MEDIUM",
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
        )
    ),
    system_instruction=types.Content(
        parts=[types.Part.from_text(text="""
You are Maya, a female donor relationship executive from ImpactGuru.
Speak warmly, empathetically, and clearly to donors. Your role is to understand
why their donation failed, help them retry, and show gratitude for their support.
Always use polite conversational Hindi-English mix (Hinglish).
""")]
    )
)

# ==================================================
# GEMINI HANDLER
# ==================================================
class GeminiBridge:
    def __init__(self, exotel_ws: WebSocket, sample_rate: int = 8000):
        self.exotel_ws = exotel_ws
        self.session = None
        self.running = True
        self.sample_rate = sample_rate
        self._ratecv_state = None  # for smooth downsampling

    # Send the first spoken greeting
    async def _send_initial_greeting(self):
        try:
            initial_text = (
                "Namaste, ImpactGuru mein call karne ke liye dhanyavaad. "
                "Main Maya, aapki kaise sahayata kar sakti hoon?"
            )
            logging.info(f"-> Sending initial text to Gemini: '{initial_text}'")

            # Ensure Gemini generates audio immediately
            await self.session.send(input=initial_text, end_of_turn=True)

            logging.info("-> Initial greeting sent successfully.")
        except Exception as e:
            logging.error(f"❌ Failed to send initial greeting: {e}", exc_info=True)

    async def start(self):
        logging.info("Connecting to Gemini Live session...")
        try:
            async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
                self.session = session
                logging.info("✅ Connected to Gemini Live Audio Session.")

                await self._send_initial_greeting()

                await asyncio.gather(
                    self.forward_exotel_to_gemini(),
                    self.forward_gemini_to_exotel(),
                )

        except Exception as e:
            logging.error(f"❌ Error in GeminiBridge.start: {e}", exc_info=True)
        finally:
            self.running = False
            if self.session and not self.session.closed:
                await self.session.close()
                logging.info("🧹 Gemini session closed.")

    # Receive audio from Exotel → forward to Gemini
    async def forward_exotel_to_gemini(self):
        try:
            while self.running:
                msg = await self.exotel_ws.receive_text()
                data = json.loads(msg)
                event = data.get("event")

                if event == "media":
                    try:
                        audio_b64 = data["media"]["payload"]
                        mulaw_bytes = base64.b64decode(audio_b64)
                        # μ-law 8kHz → PCM16 8kHz
                        pcm16_8k = audioop.ulaw2lin(mulaw_bytes, 2)
                        # Up-sample 8kHz → 16kHz for Gemini
                        pcm16_16k, _ = audioop.ratecv(
                            pcm16_8k, 2, 1, 8000, 16000, None
                        )
                        await self.session.send(
                            input={"data": pcm16_16k, "mime_type": "audio/pcm"}
                        )
                    except Exception as e:
                        logging.error(f"❌ Error processing Exotel audio: {e}", exc_info=True)
                        continue

                elif event == "stop":
                    logging.info("🛑 Exotel stop event received, closing session.")
                    self.running = False
                    break

                await asyncio.sleep(0.005)
        except WebSocketDisconnect:
            logging.warning("⚠️ Exotel WebSocket disconnected.")
        except Exception as e:
            logging.error(f"❌ Error in forward_exotel_to_gemini: {e}", exc_info=True)
        finally:
            self.running = False

    # Receive Gemini audio → forward to Exotel
    async def forward_gemini_to_exotel(self):
        """Receive Gemini audio → filter non-audio → cleanly convert to Exotel μ-law."""
        try:
            self._ratecv_state = None
            BUFFER_MS = 20
            FRAME_SIZE = int(self.sample_rate * 2 * BUFFER_MS / 1000)
            buffer = bytearray()

            while self.running:
                turn = self.session.receive()
                async for response in turn:
                    # --- Filter out any non-audio events ---
                    if not hasattr(response, "data") or not response.data:
                        if getattr(response, "text", None):
                            logging.info(f"<- Gemini TEXT: {response.text}")
                        continue

                    # --- Some Gemini SDKs wrap data in a dict ---
                    data_part = response.data
                    if isinstance(data_part, dict):
                        if data_part.get("mime_type") != "audio/pcm":
                            logging.debug(f"Skipping non-audio mime_type: {data_part.get('mime_type')}")
                            continue
                        audio_bytes = data_part.get("data", b"")
                        import wave
                        with wave.open("gemini_raw.wav", "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(16000)
                            wf.writeframes(audio_bytes)
                        break

                    elif isinstance(data_part, (bytes, bytearray)):
                        audio_bytes = data_part
                    else:
                        logging.debug(f"Skipping unexpected data type: {type(data_part)}")
                        continue

                    if not audio_bytes:
                        continue

                
                    pcm16_target = audio_bytes  # no resample
                    audio_b64 = base64.b64encode(pcm16_target).decode()
                    await self.exotel_ws.send_text(json.dumps({
                        "event": "media",
                        "stream_sid": "gemini_stream",
                        "media": {"payload": audio_b64}
                    }))

                await asyncio.sleep(0.002)

        except Exception as e:
            logging.error(f"❌ Error in forward_gemini_to_exotel: {e}", exc_info=True)
        finally:
            self.running = False
            if self.session and not self.session.closed:
                await self.session.close()
                logging.info("🧹 Gemini session closed.")

# ==================================================
# FASTAPI ROUTES
# ==================================================
@app.websocket("/ws/audio")
async def exotel_audio(websocket: WebSocket):
    await websocket.accept()

    # Extract ?sample-rate= param if passed
    query = parse_qs(websocket.url.query)
    try:
        sample_rate = int(query.get("sample-rate", ["8000"])[0])
    except ValueError:
        sample_rate = 8000

    logging.info(f"🎧 WebSocket connected (sample-rate={sample_rate} Hz)")

    handler = GeminiBridge(websocket, sample_rate=sample_rate)
    try:
        await handler.start()
    finally:
        if handler.session and not handler.session.closed:
            await handler.session.close()
        await websocket.close()
        logging.info("🔒 Exotel WebSocket closed.")

@app.get("/")
def root():
    return {"message": "✅ Exotel ⇄ Gemini Native Audio Bridge running"}