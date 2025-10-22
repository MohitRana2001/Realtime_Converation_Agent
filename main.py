import os
import json
import base64
import asyncio
import logging
import audioop
from typing import Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from urllib.parse import parse_qs

from google import genai
from google.genai import types
from google.genai.types import AudioTranscriptionConfig

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

MODEL = "gemini-2.5-flash-native-audio-preview-09-2025"

client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY,
)

# ==================================================
# GEMINI CONFIG
# ==================================================
CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
        ),
        language_code="hi-IN",
    ),
    system_instruction=types.Content(
        parts=[types.Part.from_text(text="""
You are Maya, a female donor relationship executive from ImpactGuru.
Speak warmly, empathetically, and clearly to donors. Your role is to understand
why their donation failed, help them retry, and show gratitude for their support.
Always use polite conversational Hindi-English mix (Hinglish).

IMPORTANT: You must respond with AUDIO speech only. Do not include any text thoughts or explanations.
Just speak your response naturally in Hinglish.
""")]
    ),
    output_audio_transcription=AudioTranscriptionConfig()
)

# ==================================================
# Helpers
# ==================================================
def resample_pcm16(
    pcm: bytes, src_rate: int, dst_rate: int, state: Optional[bytes] = None
) -> Tuple[bytes, Optional[bytes]]:
    """Resample 16-bit little-endian mono PCM."""
    if src_rate == dst_rate:
        return pcm, state
    out, new_state = audioop.ratecv(pcm, 2, 1, src_rate, dst_rate, state)
    return out, new_state

# ==================================================
# GEMINI HANDLER
# ==================================================
class GeminiBridge:
    """Bridges WebSocket client <-> Gemini Live API for bidirectional audio streaming."""
    
    def __init__(self, exotel_ws: WebSocket, hinted_sample_rate: int = 16000):
        self.exotel_ws = exotel_ws
        self.session = None
        self.running = True
        self.exotel_stream_sid: Optional[str] = None
        self.exotel_rate: int = hinted_sample_rate
        self._to_16k_state = None
        self._to_client_state = None
        self._current_chunks = 0

    async def _send_initial_greeting(self):
        initial_text = (
            "Namaste, ImpactGuru mein call karne ke liye dhanyavaad. "
            "Main Maya bol rahi hoon — aapki kaise sahayata kar sakti hoon?"
        )
        logging.info("-> Sending initial greeting to Gemini")
        await self.session.send_realtime_input(text=initial_text)

    async def start(self):
        logging.info("Connecting to Gemini Live session...")
        try:
            async with client.aio.live.connect(model=MODEL, config=CONFIG) as session:
                self.session = session
                logging.info("✅ Connected to Gemini Live session.")

                try:
                    await self._send_initial_greeting()
                    logging.info("✅ Initial greeting sent successfully")
                except Exception as e:
                    logging.error(f"❌ Failed to send initial greeting: {e}", exc_info=True)

                await asyncio.gather(
                    self.forward_exotel_to_gemini(),
                    self.forward_gemini_to_exotel(),
                    return_exceptions=True
                )

        except Exception as e:
            logging.error(f"❌ Error in GeminiBridge.start: {e}", exc_info=True)
        finally:
            self.running = False
            if self.session:
                try:
                    await self.session.close()
                    logging.info("🧹 Gemini session closed.")
                except Exception:
                    pass

    async def forward_exotel_to_gemini(self):
        logging.info("🎙️ Starting to forward client audio to Gemini...")
        try:
            while self.running:
                raw = await self.exotel_ws.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "connected":
                    logging.info("🔗 Exotel WS connected.")
                    continue

                if event == "start":
                    self.exotel_stream_sid = data.get("stream_sid") or data.get("start", {}).get("stream_sid")
                    sr = data.get("start", {}).get("media_format", {}).get("sample_rate")
                    if isinstance(sr, int) and sr > 0:
                        self.exotel_rate = sr
                    logging.info(f"🎧 Stream start: sid={self.exotel_stream_sid}, rate={self.exotel_rate} Hz")
                    continue

                if event == "media":
                    if not self.session:
                        logging.warning("⚠️ Session not available, skipping audio chunk")
                        continue
                        
                    try:
                        b64 = data["media"]["payload"]
                        pcm_exotel = base64.b64decode(b64)
                        pcm_16k, self._to_16k_state = resample_pcm16(
                            pcm_exotel, self.exotel_rate, 16000, self._to_16k_state
                        )
                        await self.session.send_realtime_input(
                            audio={"data": pcm_16k, "mime_type": "audio/pcm;rate=16000"}
                        )
                        self._current_chunks += 1
                    except Exception as e:
                        logging.error(f"❌ Error processing audio: {e}")
                        if "1000" in str(e) or "closed" in str(e).lower():
                            logging.error("Session closed, stopping")
                            self.running = False
                            break
                        continue
                
                elif event == "mark":
                    mark_name = data.get("mark", {}).get("name", "")
                    if mark_name == "turn_complete" and self._current_chunks > 0:
                        logging.info(f"🔵 Turn complete ({self._current_chunks} chunks)")
                        self._current_chunks = 0
                    continue

                elif event == "dtmf":
                    logging.info(f"☎️ DTMF: {data.get('dtmf')}")
                    continue

                elif event == "stop":
                    logging.info("🛑 Stop event received")
                    self.running = False
                    break

                await asyncio.sleep(0)
        except WebSocketDisconnect:
            logging.warning("⚠️ WebSocket disconnected")
        except Exception as e:
            logging.error(f"❌ Error in forward_exotel_to_gemini: {e}", exc_info=True)
        finally:
            logging.info("🔚 Exotel→Gemini loop finished")
            self.running = False

    async def forward_gemini_to_exotel(self):
        """Receive Gemini audio (24kHz) and stream to client (8kHz)."""
        try:
            logging.info("👂 Listening for Gemini responses...")
            bytes_per_sec = self.exotel_rate * 2
            frame20 = int(bytes_per_sec * 0.020)
            min_flush = int(bytes_per_sec * 0.100)
            buffer = bytearray()

            while self.running:
                try:
                    async for response in self.session.receive():
                        server_content = response.server_content
                        if not server_content:
                            continue
                        
                        model_turn = server_content.model_turn
                        turn_complete = server_content.turn_complete
                        output_transcription = server_content.output_transcription
                        audio_bytes = None
                        
                        if model_turn:
                            for part in model_turn.parts:
                                if hasattr(part, 'inline_data') and part.inline_data is not None:
                                    audio_bytes = part.inline_data.data
                                    break
                                if hasattr(part, 'text') and part.text:
                                    logging.info(f"<- TEXT: {part.text}")
                        
                        if output_transcription and hasattr(output_transcription, 'text'):
                            text = output_transcription.text
                            if text and isinstance(text, str) and text.strip():
                                logging.info(f"<- TRANSCRIPT: {text}")
                        
                        if turn_complete:
                            logging.info("✅ Turn complete")

                        if not audio_bytes:
                            continue

                        # Gemini outputs 24kHz, resample to client rate (16kHz for better quality)
                        pcm_client, self._to_client_state = resample_pcm16(
                            audio_bytes, 24000, self.exotel_rate, self._to_client_state
                        )

                        buffer.extend(pcm_client)
                        while len(buffer) >= min_flush:
                            flush_size = (len(buffer) // frame20) * frame20
                            if flush_size < min_flush:
                                break
                            chunk = buffer[:flush_size]
                            del buffer[:flush_size]
                            payload = base64.b64encode(chunk).decode("ascii")
                            await self._send_exotel_media(payload)

                        await asyncio.sleep(0)

                    if buffer:
                        payload = base64.b64encode(buffer).decode("ascii")
                        await self._send_exotel_media(payload)
                        buffer.clear()
                
                except Exception as e:
                    logging.error(f"❌ Error receiving from Gemini: {e}", exc_info=True)
                    await asyncio.sleep(0.1)
            
            logging.info("📭 Receive loop completed")

        except Exception as e:
            logging.error(f"❌ Error in forward_gemini_to_exotel: {e}", exc_info=True)
        finally:
            logging.info("🔚 Gemini→Exotel loop finished")
            self.running = False

    async def _send_exotel_media(self, payload_b64: str):
        if not self.exotel_stream_sid:
            self.exotel_stream_sid = "default_stream"
        msg = {
            "event": "media",
            "stream_sid": self.exotel_stream_sid,
            "media": {"payload": payload_b64}
        }
        await self.exotel_ws.send_text(json.dumps(msg))

@app.websocket("/ws/audio")
async def exotel_audio(websocket: WebSocket):
    await websocket.accept()
    query = parse_qs(websocket.url.query)
    try:
        hinted_rate = int(query.get("sample-rate", ["16000"])[0])
    except ValueError:
        hinted_rate = 16000  # Use 16kHz for better quality

    logging.info(f"🎧 WebSocket connected (sample-rate={hinted_rate} Hz)")

    handler = GeminiBridge(websocket, hinted_sample_rate=hinted_rate)
    try:
        await handler.start()
    finally:
        if handler.session:
            try:
                await handler.session.close()
            except Exception:
                pass
        await websocket.close()
        logging.info("🔒 WebSocket closed")

@app.get("/")
def root():
    return {"message": "✅ Gemini Live Audio Bridge running"}
