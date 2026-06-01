import * as vscode from 'vscode';
import { execFile } from 'child_process';

/**
 * Text-to-Speech provider with two tiers:
 *
 *  1. **Local** — macOS `say` command. Free, instant, offline-capable.
 *     Used for short responses (status updates, confirmations, errors).
 *
 *  2. **Cloud** — Gemini 2.5 Flash TTS via REST API. Natural-sounding,
 *     supports style prompting and multi-speaker. Used for longer technical
 *     explanations when a Gemini API key is available.
 *
 * The provider automatically routes based on text length and key availability.
 */

/** Read settings from VS Code configuration. */
function getConfig() {
    const config = vscode.workspace.getConfiguration('antigravity');
    return {
        threshold: config.get<number>('tts.localThreshold', 200),
        localVoice: config.get<string>('tts.localVoice', 'Samantha'),
        geminiVoice: config.get<string>('tts.geminiVoice', 'Kore'),
        preferredTier: config.get<string>('tts.preferredTier', 'auto'),
    };
}

export class TTSProvider implements vscode.Disposable {
    private currentProcess: ReturnType<typeof execFile> | undefined;
    private speaking = false;

    constructor(private readonly context: vscode.ExtensionContext) {}

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /**
     * Speak the given text, automatically choosing the best TTS tier.
     * Returns a promise that resolves when speech finishes (or is interrupted).
     */
    async speak(text: string, options?: { forceLocal?: boolean; forceCloud?: boolean }): Promise<void> {
        // Cancel any in-progress speech first.
        this.stop();

        if (!text.trim()) {
            return;
        }

        const cfg = getConfig();
        const useCloud =
            options?.forceCloud ||
            (!options?.forceLocal &&
                (cfg.preferredTier === 'cloud' ||
                    (cfg.preferredTier === 'auto' && text.length >= cfg.threshold)) &&
                (await this.hasGeminiKey()));

        if (useCloud) {
            return this.speakGemini(text);
        }
        return this.speakLocal(text);
    }

    /** Interrupt any in-progress speech immediately. */
    stop(): void {
        if (this.currentProcess) {
            try {
                this.currentProcess.kill();
            } catch {
                /* already exited */
            }
            this.currentProcess = undefined;
        }
        this.speaking = false;
    }

    get isSpeaking(): boolean {
        return this.speaking;
    }

    dispose(): void {
        this.stop();
    }

    // ------------------------------------------------------------------
    // Tier 1: macOS `say`
    // ------------------------------------------------------------------

    private speakLocal(text: string): Promise<void> {
        return new Promise<void>((resolve) => {
            this.speaking = true;

            const cfg = getConfig();
            const args = ['-v', cfg.localVoice];

            const child = execFile('say', args, (err) => {
                this.speaking = false;
                this.currentProcess = undefined;
                if (err && (err as any).killed) {
                    // Interrupted by stop() — not an error.
                    resolve();
                    return;
                }
                if (err) {
                    // Voice not found? Fall back to default voice.
                    const fallback = execFile('say', [], (err2) => {
                        this.speaking = false;
                        this.currentProcess = undefined;
                        resolve();
                    });
                    fallback.stdin?.write(text);
                    fallback.stdin?.end();
                    this.currentProcess = fallback;
                    return;
                }
                resolve();
            });

            child.stdin?.write(text);
            child.stdin?.end();
            this.currentProcess = child;
        });
    }

    // ------------------------------------------------------------------
    // Tier 2: Gemini 2.5 Flash TTS
    // ------------------------------------------------------------------

    private async speakGemini(text: string): Promise<void> {
        const apiKey = await this.getGeminiKey();
        if (!apiKey) {
            // Fall back to local if key disappeared between check and use.
            return this.speakLocal(text);
        }

        this.speaking = true;

        try {
            const audioBase64 = await this.callGeminiTTS(apiKey, text);

            if (!audioBase64) {
                // API returned no audio — fall back to local.
                return this.speakLocal(text);
            }

            // Write audio to temp file and play with afplay (macOS).
            await this.playAudioBase64(audioBase64);
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            vscode.window.showWarningMessage(`Gemini TTS failed, using local voice: ${msg}`);
            return this.speakLocal(text);
        } finally {
            this.speaking = false;
        }
    }

    /**
     * Call Gemini 2.5 Flash with a TTS request.
     * Uses the generateContent endpoint with response_modalities: ["AUDIO"].
     */
    private async callGeminiTTS(apiKey: string, text: string): Promise<string | undefined> {
        const url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent';

        const body = {
            contents: [
                {
                    parts: [
                        {
                            text: `Read the following aloud in a clear, professional, encouraging tone suitable for a developer receiving feedback from an AI assistant:\n\n${text}`,
                        },
                    ],
                },
            ],
            generationConfig: {
                response_modalities: ['AUDIO'],
                speech_config: {
                    voice_config: {
                        prebuilt_voice_config: {
                            voice_name: getConfig().geminiVoice,
                        },
                    },
                },
            },
        };

        // Use Node's built-in fetch (available in Node 18+, which VS Code ships).
        // API key goes in a header, not the URL, to avoid leaking it into logs.
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-goog-api-key': apiKey,
            },
            body: JSON.stringify(body),
        });

        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`Gemini TTS API ${response.status}: ${errorText.slice(0, 200)}`);
        }

        const json = await response.json() as any;

        // Navigate the Gemini response structure to find audio data.
        const parts = json?.candidates?.[0]?.content?.parts;
        if (!parts || parts.length === 0) {
            return undefined;
        }

        // The audio part contains inlineData with base64-encoded audio.
        for (const part of parts) {
            if (part.inlineData?.data) {
                return part.inlineData.data;
            }
        }

        return undefined;
    }

    /**
     * Decode base64 audio, write to temp file, play with `afplay`.
     */
    private playAudioBase64(base64Audio: string): Promise<void> {
        return new Promise<void>((resolve, reject) => {
            const fs = require('fs');
            const os = require('os');
            const path = require('path');

            const tmpFile = path.join(os.tmpdir(), `antigravity-tts-${Date.now()}.wav`);
            const audioBuffer = Buffer.from(base64Audio, 'base64');
            fs.writeFileSync(tmpFile, audioBuffer);

            const child = execFile('afplay', [tmpFile], (err) => {
                // Clean up temp file.
                try {
                    fs.unlinkSync(tmpFile);
                } catch {
                    /* ignore */
                }

                this.currentProcess = undefined;
                if (err && (err as any).killed) {
                    resolve(); // interrupted
                } else if (err) {
                    reject(err);
                } else {
                    resolve();
                }
            });

            this.currentProcess = child;
        });
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private async getGeminiKey(): Promise<string | undefined> {
        return this.context.secrets.get('antigravity.geminiKey');
    }

    private async hasGeminiKey(): Promise<boolean> {
        return !!(await this.getGeminiKey());
    }
}
