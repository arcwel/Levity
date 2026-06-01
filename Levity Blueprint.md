# **Voice-Driven AI Development Service Plan**

## **1\. Core Workflow**

1. **Trigger:**  
   * **Active:** User presses the dedicated key command (e.g., Ctrl+Alt+V) to start "Walky-Talky" mode.  
   * **Passive:** Local **Wake-Word Engine** (Picovoice) detects "Hey Antigravity."  
2. **Transcription:** Audio is sent to **Whisper (Local)** or **Gemini (Cloud)** using the user's provided API key.  
3. **Execution:** The agent processes the code or question.  
4. **TTS Strategy Selection:**  
   * **Short/System Status:** Use **Web Speech API** with the system's "Premium" local voice.  
   * **Detailed Technical Summaries:** Use **Gemini 2.5 Flash TTS** for its technical emphasis and multi-speaker capabilities.

## **2\. Technical Stack Recommendation**

| Component | Recommended Tool | Cost / Strategy |
| :---- | :---- | :---- |
| **Wake-Word** | Picovoice Porcupine | Local processing (Free for Personal use). |
| **STT** | OpenAI Whisper (Tiny) | **Free** (Local) or **BYOK** (Cloud API). |
| **Primary TTS** | Web Speech API | **Free** (Uses OS voices like Siri/Microsoft Neural). |
| **Advanced TTS** | Gemini 2.5 Flash TTS | **BYOK** (User provides Gemini API Key). |
| **Key Storage** | vscode.SecretStorage | **Secure:** Encrypts keys in the OS Keychain/Credential Manager. |

## **3\. Implementation: Using System Voices**

To implement "Premium" sounding local voices in your VS Code fork, use the following logic in your VoiceProvider:

// Function to get the best available local voices  
const getPremiumSystemVoices \= () \=\> {  
  const voices \= window.speechSynthesis.getVoices();  
  // Filter for 'Premium' or 'Neural' strings (e.g., Siri, Microsoft Neural)  
  return voices.filter(v \=\> v.name.includes('Premium') || v.name.includes('Neural'));  
};

const speakLocally \= (text, selectedVoice) \=\> {  
  const utterance \= new SpeechSynthesisUtterance(text);  
  // Default to the first premium voice found  
  utterance.voice \= selectedVoice || getPremiumSystemVoices();  
  window.speechSynthesis.speak(utterance);  
};

## **4\. Key Integration Features**

### **Bring Your Own Key (BYOK)**

Users provide their own Gemini API key via the Command Palette. The extension stores this securely:

// Example storage logic in the extension  
async function saveApiKey(context, key) {  
    await context.secrets.store('antigravity.geminiKey', key);  
}

### **Manual Trigger (Key Command)**

A default keybinding is registered in the package.json to allow quick "Tap-to-Talk" or "Hold-to-Talk" functionality:

* **Command:** antigravity.voice.start  
* **Default Windows/Linux:** Ctrl+Alt+V  
* **Default macOS:** Cmd+Alt+V

## **5\. Why Gemini 2.5 Flash TTS is the "Pro" Upgrade**

If the local voices aren't "natural" enough for the user, Gemini 2.5 Flash TTS offers:

* **Natural Style Prompting:** Change the tone dynamically: *"Explain the fix with an encouraging, professional tone."*  
* **Multi-Speaker Support:** Differentiates between "System Voice" (reading logs) and "Agent Voice" (answering questions) in one stream.

## **6\. Development Phases**

### Phase 1: Scaffold (COMPLETE)
- VS Code extension skeleton with TypeScript
- BYOK Gemini API key storage via SecretStorage
- Keybindings (Cmd+Alt+V) and command palette entries

### Phase 2: Audio Capture + STT (COMPLETE)
- Python sidecar (`sidecar/voice_worker.py`) with JSON-over-stdio protocol
- Microphone capture via `sounddevice` (16kHz mono float32)
- Local Whisper STT (`openai-whisper` Python package, base model)
- `SidecarManager` TypeScript class spawns/manages Python process
- Toggle recording via Cmd+Alt+V (start/stop)
- Status bar indicator with recording state feedback
- Transcription result → insert at cursor, copy, or send to Gemini
- Setup script (`sidecar/setup.sh`) creates venv + installs deps

### Phase 3: TTS Response (COMPLETE)
- `TTSProvider` (`src/ttsProvider.ts`) — two-tier TTS with automatic routing
  - **Local:** macOS `say` command (free, instant, offline). Used for responses < 200 chars
  - **Cloud:** Gemini 2.5 Flash TTS via REST API (voice: Kore, professional tone). Used for longer responses when API key is set
  - Graceful fallback: cloud failure → local `say`
  - Interrupt support: click status bar or Cmd+Alt+V to stop playback
- `GeminiService` (`src/geminiService.ts`) — Gemini 2.5 Flash text API for processing voice commands
  - System prompt tuned for spoken responses (concise, no markdown)
  - Active editor context injection (selected text or cursor window)
  - Full response logged to "Antigravity Response" output channel
- Updated `VoiceProvider` — full pipeline: Listen → Transcribe → Think → Speak
  - Status bar: Ready → Listening → Transcribing → Thinking → Speaking → Ready
  - Without Gemini key: echo-back mode (just speaks the transcription)
  - With Gemini key: AI-powered voice assistant with contextual code awareness

### Phase 4: Wake-Word + Polish (COMPLETE)
- `WakeWordManager` (in `voice_worker.py`) — Picovoice Porcupine integration
  - 15 built-in keywords (Computer, Jarvis, Alexa, Hey Siri, etc.)
  - Custom .ppn keyword file support (create "Hey Antigravity" at console.picovoice.ai)
  - Auto-starts recording when wake word detected
  - Separate int16 audio stream for Porcupine (doesn't interfere with float32 Whisper stream)
- Settings webview panel (`src/settingsPanel.ts`) — full visual configuration UI
  - **Trigger Mode**: tap-to-talk / hold-to-talk / wake-word
  - **Wake Word**: keyword selection, custom .ppn path, Picovoice key management
  - **STT**: Whisper model size (tiny/base/small/medium), silence timeout
  - **TTS**: engine preference (auto/local/cloud), local voice name, Gemini voice, character threshold
  - **API Keys**: Gemini and Picovoice key management with status indicators
  - Test button for local TTS, link to VS Code keybinding editor
- VS Code `configuration` settings — all preferences in settings.json
- Runtime config sync — changing settings hot-reloads Whisper model + silence duration
- Status bar: shows wake-word keyword when in always-on mode ("Say 'computer'")

## **7\. Future: Offline Mode (Planned)**

A fully offline tier is recommended for future development:
- **STT:** Whisper.cpp with bundled model (~150MB, runs on-device)
- **TTS:** macOS `say` (already offline) or Mozilla Piper for Linux
- **AI Processing:** Ollama with a local model (Llama 3.2 3B or Phi-3 Mini) for command interpretation
- **Wake-word:** Porcupine already runs offline
- **Pattern matching:** Direct command-to-action mapping for common operations ("run tests", "format file") bypassing the LLM entirely
- Auto-detect network availability and fall through tiers: Offline → Local+API → Full Cloud