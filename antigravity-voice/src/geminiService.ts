import * as vscode from 'vscode';

/**
 * Thin wrapper around the Gemini 2.5 Flash text API for processing
 * voice commands — interpreting user intent, answering coding questions,
 * generating code, etc.
 *
 * The TTS layer is handled separately by TTSProvider; this service
 * returns plain text responses.
 */

const MODEL = 'gemini-2.5-flash-preview-05-20';
const GENERATE_URL = `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent`;

/** System prompt that shapes Gemini's responses for voice interaction. */
const SYSTEM_INSTRUCTION = `You are Antigravity, a voice-driven AI coding assistant embedded in VS Code.
The user speaks to you and hears your reply read aloud, so keep responses concise and conversational.

Guidelines:
- Be brief. Aim for 1-3 sentences for simple answers, up to a short paragraph for explanations.
- When explaining code, focus on the *why*, not line-by-line narration.
- If the user asks you to write or edit code, describe what you'll do in one sentence, then output the code.
- Use natural spoken language — avoid markdown formatting, bullet lists, or headers since the response will be spoken aloud.
- If you're unsure what the user wants, ask a short clarifying question.`;

export interface GeminiResponse {
    text: string;
    /** Approximate token count for cost tracking. */
    tokenCount?: number;
}

export class GeminiService implements vscode.Disposable {
    constructor(private readonly context: vscode.ExtensionContext) {}

    /**
     * Send a user message to Gemini and get a text response.
     * Includes the active editor context if available.
     */
    async chat(userMessage: string): Promise<GeminiResponse> {
        const apiKey = await this.getApiKey();
        if (!apiKey) {
            throw new Error('Gemini API key not set. Run "Antigravity: Set Gemini API Key".');
        }

        // Build context from the active editor.
        const editorContext = this.getEditorContext();

        const parts: Array<{ text: string }> = [];
        if (editorContext) {
            parts.push({ text: editorContext });
        }
        parts.push({ text: userMessage });

        const body = {
            system_instruction: {
                parts: [{ text: SYSTEM_INSTRUCTION }],
            },
            contents: [
                {
                    role: 'user',
                    parts,
                },
            ],
            generationConfig: {
                temperature: 0.7,
                maxOutputTokens: 1024,
            },
        };

        // Pass the API key via header (not the URL query string) so it never
        // leaks into request logs, proxies, or error traces.
        const response = await fetch(GENERATE_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-goog-api-key': apiKey,
            },
            body: JSON.stringify(body),
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`Gemini API ${response.status}: ${errText.slice(0, 300)}`);
        }

        const json = await response.json() as any;

        const text =
            json?.candidates?.[0]?.content?.parts
                ?.map((p: any) => p.text ?? '')
                .join('') ?? '';

        const tokenCount =
            (json?.usageMetadata?.promptTokenCount ?? 0) +
            (json?.usageMetadata?.candidatesTokenCount ?? 0);

        return { text: text.trim(), tokenCount };
    }

    dispose(): void {
        // Nothing to clean up currently.
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private async getApiKey(): Promise<string | undefined> {
        return this.context.secrets.get('antigravity.geminiKey');
    }

    /**
     * Build a short context string from the active editor so Gemini
     * can give code-aware answers.
     */
    private getEditorContext(): string | undefined {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return undefined;
        }

        const doc = editor.document;
        const fileName = doc.fileName;
        const languageId = doc.languageId;

        // Send selected text if any, otherwise a window around the cursor.
        const selection = editor.selection;
        let snippet: string;

        if (!selection.isEmpty) {
            snippet = doc.getText(selection);
            if (snippet.length > 3000) {
                snippet = snippet.slice(0, 3000) + '\n... (truncated)';
            }
        } else {
            const cursorLine = selection.active.line;
            const startLine = Math.max(0, cursorLine - 20);
            const endLine = Math.min(doc.lineCount - 1, cursorLine + 20);
            const range = new vscode.Range(startLine, 0, endLine, doc.lineAt(endLine).text.length);
            snippet = doc.getText(range);
        }

        return [
            `[Active file: ${fileName} (${languageId})]`,
            `[Cursor at line ${selection.active.line + 1}]`,
            '```',
            snippet,
            '```',
        ].join('\n');
    }
}
