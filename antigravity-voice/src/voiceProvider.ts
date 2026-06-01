import * as vscode from 'vscode';
import { SidecarManager } from './sidecarManager';
import { TTSProvider } from './ttsProvider';
import { GeminiService } from './geminiService';

/**
 * Drives the full voice workflow:
 *   1. Listen (sidecar → Whisper STT)
 *   2. Process (Gemini text API — if key available)
 *   3. Respond (TTS — local `say` or Gemini TTS)
 *
 * Supports three trigger modes:
 *   - **tap**: press hotkey to start, press again to stop
 *   - **hold**: (planned — wired at keybinding level)
 *   - **wakeWord**: always-on Porcupine detection → auto-record
 */
export class VoiceProvider {
    private busy = false;
    private readonly tts: TTSProvider;
    private readonly gemini: GeminiService;
    private wakeWordActive = false;
    private responseEnabled = true;
    private serverStopped = false;
    /** Single reusable output channel for response logs (created lazily). */
    private responseChannel: vscode.OutputChannel | undefined;

    constructor(
        private readonly context: vscode.ExtensionContext,
        private readonly sidecar: SidecarManager,
        private readonly statusBar: vscode.StatusBarItem,
    ) {
        this.tts = new TTSProvider(context);
        this.gemini = new GeminiService(context);
        this.setIdle();

        // Mirror sidecar-side state changes into the status bar.
        this.context.subscriptions.push(
            this.sidecar.onState((state) => {
                if (state === 'listening') {
                    this.setListening();
                } else if (state === 'processing') {
                    this.setTranscribing();
                } else if (state === 'wakeword') {
                    this.setWakeWordIdle();
                }
                // 'idle' handled after full workflow completes.
            }),
            this.sidecar.onError((message) => {
                vscode.window.showErrorMessage(`Antigravity Voice: ${message}`);
                this.busy = false;
                if (this.wakeWordActive) {
                    this.setWakeWordIdle();
                } else {
                    this.setIdle();
                }
            }),
            // Wake-word detected → auto-start the workflow.
            this.sidecar.onWakeWord(() => {
                this.onWakeWordDetected();
            }),
            // Listen for transcriptions during wake-word auto-record.
            this.sidecar.onTranscription((text) => {
                if (this.busy) {
                    // Handled by awaitNextTranscription in triggerVoiceWorkflow.
                    return;
                }
                // This fires from wake-word auto-record.
                this.handleAutoTranscription(text);
            }),
        );
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    async triggerVoiceWorkflow(): Promise<void> {
        // If currently speaking, stop playback and reset.
        if (this.tts.isSpeaking) {
            this.tts.stop();
            this.busy = false;
            this.resetToBaseState();
            return;
        }

        if (this.busy) {
            vscode.window.showInformationMessage('Antigravity: already listening.');
            return;
        }

        this.busy = true;
        this.setListening();

        const transcription = this.awaitNextTranscription();

        const started = await this.sidecar.startListening();
        if (!started) {
            this.busy = false;
            this.resetToBaseState();
            return;
        }

        const text = await transcription;
        await this.processTranscription(text);
    }

    stopListening(): void {
        if (this.tts.isSpeaking) {
            this.tts.stop();
            this.busy = false;
            this.resetToBaseState();
            return;
        }
        if (!this.busy) {
            return;
        }
        this.sidecar.stopListening();
    }

    /** Apply all settings from VS Code configuration to the sidecar and local state. */
    applySettings(): void {
        const config = vscode.workspace.getConfiguration('antigravity');

        // Sync Whisper model and silence duration to the sidecar.
        this.sidecar.configure({
            whisper_model: config.get('whisper.model', 'base'),
            silence_duration: config.get('silenceDuration', 10),
        });

        // Apply trigger mode (may start/stop wake-word).
        this.applyTriggerMode();
    }

    /** Start or stop wake-word detection based on the current trigger mode setting. */
    async applyTriggerMode(): Promise<void> {
        const config = vscode.workspace.getConfiguration('antigravity');
        const mode = config.get<string>('triggerMode', 'tap');

        if (mode === 'wakeWord') {
            const accessKey = await this.context.secrets.get('antigravity.picovoiceKey');
            if (!accessKey) {
                vscode.window.showWarningMessage(
                    'Antigravity: Wake-word mode requires a Picovoice access key. ' +
                    'Run "Antigravity: Set Picovoice Access Key" or switch to tap/hold mode.',
                );
                this.wakeWordActive = false;
                this.setIdle();
                return;
            }

            const keyword = config.get<string>('wakeWord.keyword', 'computer');
            const customPath = config.get<string>('wakeWord.customKeywordPath', '');

            this.wakeWordActive = true;
            await this.sidecar.startWakeWord(accessKey, keyword, customPath);
        } else {
            if (this.wakeWordActive) {
                this.wakeWordActive = false;
                this.sidecar.stopWakeWord();
            }
            this.setIdle();
        }
    }

    /** Enable or disable voice responses (TTS). */
    setResponseEnabled(enabled: boolean): void {
        this.responseEnabled = enabled;
    }

    /** Whether voice responses are currently enabled. */
    getResponseEnabled(): boolean {
        return this.responseEnabled;
    }

    /** Mark the server as manually stopped (updates status bar). */
    setServerStopped(stopped: boolean): void {
        this.serverStopped = stopped;
    }

    dispose(): void {
        this.tts.dispose();
        this.gemini.dispose();
        this.responseChannel?.dispose();
        this.responseChannel = undefined;
    }

    // ------------------------------------------------------------------
    // Wake-word handling
    // ------------------------------------------------------------------

    private async onWakeWordDetected(): Promise<void> {
        if (this.busy) {
            return; // already in a workflow
        }

        // Mark busy before any await so a transcription emitted by the
        // already-running auto-record can't slip through to the global handler.
        this.busy = true;

        // Play a short confirmation prompt via local TTS (fire-and-forget,
        // but swallow rejections so they don't surface as unhandled).
        this.tts.speak('Yes?', { forceLocal: true }).catch(() => { /* non-fatal */ });

        // The sidecar already auto-started recording on wake-word detection,
        // so we just wait for the transcription to arrive.
    }

    private async handleAutoTranscription(text: string): Promise<void> {
        if (!this.busy) {
            return;
        }
        await this.processTranscription(text);
    }

    // ------------------------------------------------------------------
    // Shared transcription → response pipeline
    // ------------------------------------------------------------------

    private async processTranscription(text: string | undefined): Promise<void> {
        if (text === undefined) {
            this.busy = false;
            this.resetToBaseState();
            return;
        }
        if (!text) {
            vscode.window.showInformationMessage('Antigravity: no speech detected.');
            this.busy = false;
            this.resetToBaseState();
            return;
        }

        vscode.window.showInformationMessage(`You said: ${text}`);

        const apiKey = await this.context.secrets.get('antigravity.geminiKey');

        if (apiKey) {
            await this.processWithGemini(text);
        } else if (this.responseEnabled) {
            await this.tts.speak(`You said: ${text}`, { forceLocal: true });
        }

        this.busy = false;
        this.resetToBaseState();
    }

    private async processWithGemini(userText: string): Promise<void> {
        this.setThinking();

        try {
            const response = await this.gemini.chat(userText);

            if (!response.text) {
                if (this.responseEnabled) {
                    await this.tts.speak("I didn't get a response. Try again.", { forceLocal: true });
                }
                return;
            }

            const preview = response.text.length > 200
                ? response.text.slice(0, 200) + '...'
                : response.text;
            vscode.window.showInformationMessage(`Antigravity: ${preview}`);

            // Log full response to a single reusable channel (created once).
            if (!this.responseChannel) {
                this.responseChannel = vscode.window.createOutputChannel('Antigravity Response');
            }
            const output = this.responseChannel;
            output.appendLine(`--- ${new Date().toISOString()} ---`);
            output.appendLine(`User: ${userText}`);
            output.appendLine(`Antigravity: ${response.text}`);
            if (response.tokenCount) {
                output.appendLine(`(tokens: ~${response.tokenCount})`);
            }
            output.appendLine('');

            if (this.responseEnabled) {
                this.setSpeaking();
                await this.tts.speak(response.text);
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            vscode.window.showErrorMessage(`Antigravity: ${msg}`);
            if (this.responseEnabled) {
                await this.tts.speak('Sorry, something went wrong.', { forceLocal: true });
            }
        }
    }

    // ------------------------------------------------------------------
    // Transcription helper
    // ------------------------------------------------------------------

    private awaitNextTranscription(): Promise<string | undefined> {
        return new Promise<string | undefined>((resolve) => {
            const transcriptionSub = this.sidecar.onTranscription((text) => {
                transcriptionSub.dispose();
                errorSub.dispose();
                resolve(text);
            });
            const errorSub = this.sidecar.onError(() => {
                transcriptionSub.dispose();
                errorSub.dispose();
                resolve(undefined);
            });
        });
    }

    // ------------------------------------------------------------------
    // Status bar states
    // ------------------------------------------------------------------

    /** Return to the correct "resting" state based on whether wake-word is active. */
    private resetToBaseState(): void {
        if (this.wakeWordActive) {
            this.setWakeWordIdle();
        } else {
            this.setIdle();
        }
    }

    private setIdle(): void {
        if (this.serverStopped) {
            this.statusBar.text = '$(circle-slash) Antigravity: Off';
            this.statusBar.tooltip = 'Voice server is stopped';
            this.statusBar.command = 'antigravity.server.start';
            this.statusBar.backgroundColor = undefined;
            this.statusBar.show();
            return;
        }
        const muted = this.responseEnabled ? '' : ' (muted)';
        this.statusBar.text = `$(mic) Antigravity: Ready${muted}`;
        this.statusBar.tooltip = 'Click to start voice command';
        this.statusBar.command = 'antigravity.voice.start';
        this.statusBar.backgroundColor = undefined;
        this.statusBar.show();
    }

    private setWakeWordIdle(): void {
        const config = vscode.workspace.getConfiguration('antigravity');
        const keyword = config.get<string>('wakeWord.keyword', 'computer');
        const muted = this.responseEnabled ? '' : ' (muted)';
        this.statusBar.text = `$(ear) Antigravity: Say "${keyword}"${muted}`;
        this.statusBar.tooltip = `Wake-word active — say "${keyword}" to start`;
        this.statusBar.command = 'antigravity.voice.openSettings';
        this.statusBar.backgroundColor = undefined;
        this.statusBar.show();
    }

    private setListening(): void {
        this.statusBar.text = '$(record) Antigravity: Listening...';
        this.statusBar.tooltip = 'Click to stop listening';
        this.statusBar.command = 'antigravity.voice.stop';
        this.statusBar.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        this.statusBar.show();
    }

    private setTranscribing(): void {
        this.statusBar.text = '$(sync~spin) Antigravity: Transcribing...';
        this.statusBar.tooltip = 'Transcribing audio';
        this.statusBar.command = undefined;
        this.statusBar.backgroundColor = undefined;
        this.statusBar.show();
    }

    private setThinking(): void {
        this.statusBar.text = '$(lightbulb) Antigravity: Thinking...';
        this.statusBar.tooltip = 'Processing with Gemini';
        this.statusBar.command = undefined;
        this.statusBar.backgroundColor = undefined;
        this.statusBar.show();
    }

    private setSpeaking(): void {
        this.statusBar.text = '$(unmute) Antigravity: Speaking...';
        this.statusBar.tooltip = 'Click to stop';
        this.statusBar.command = 'antigravity.voice.stop';
        this.statusBar.backgroundColor = new vscode.ThemeColor('statusBarItem.prominentBackground');
        this.statusBar.show();
    }
}
