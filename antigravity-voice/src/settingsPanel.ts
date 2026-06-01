import * as vscode from 'vscode';

/**
 * A webview-based settings panel for Antigravity Voice.
 * Provides a friendly UI for configuring wake word, trigger mode,
 * TTS preferences, and API keys — all backed by VS Code configuration
 * and SecretStorage.
 */
export class SettingsPanel {
    public static readonly viewType = 'antigravity.settings';
    private static currentPanel: SettingsPanel | undefined;
    private readonly panel: vscode.WebviewPanel;
    private disposables: vscode.Disposable[] = [];

    private constructor(
        panel: vscode.WebviewPanel,
        private readonly context: vscode.ExtensionContext,
    ) {
        this.panel = panel;
        this.panel.webview.html = this.getHtml();

        // Handle messages from the webview.
        this.panel.webview.onDidReceiveMessage(
            (msg) => this.handleMessage(msg),
            null,
            this.disposables,
        );

        this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    }

    /** Show or focus the settings panel. */
    static createOrShow(context: vscode.ExtensionContext): void {
        if (SettingsPanel.currentPanel) {
            SettingsPanel.currentPanel.panel.reveal();
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            SettingsPanel.viewType,
            'Antigravity Voice Settings',
            vscode.ViewColumn.One,
            { enableScripts: true, retainContextWhenHidden: true },
        );

        SettingsPanel.currentPanel = new SettingsPanel(panel, context);
    }

    private dispose(): void {
        SettingsPanel.currentPanel = undefined;
        this.panel.dispose();
        for (const d of this.disposables) {
            d.dispose();
        }
        this.disposables = [];
    }

    // ------------------------------------------------------------------
    // Message handling
    // ------------------------------------------------------------------

    private async handleMessage(msg: any): Promise<void> {
        const config = vscode.workspace.getConfiguration('antigravity');

        switch (msg.type) {
            case 'ready':
                // Send current settings to the webview.
                await this.sendCurrentSettings();
                break;

            case 'setSetting': {
                const { key, value } = msg;
                if (key === '__openKeybindings') {
                    // Special pseudo-setting: open the keybinding editor.
                    vscode.commands.executeCommand(
                        'workbench.action.openGlobalKeybindings',
                        'antigravity.voice',
                    );
                } else {
                    await config.update(key, value, vscode.ConfigurationTarget.Global);
                }
                break;
            }

            case 'setGeminiKey': {
                const key = msg.value as string;
                if (key) {
                    await this.context.secrets.store('antigravity.geminiKey', key);
                    this.panel.webview.postMessage({ type: 'keyStatus', key: 'gemini', set: true });
                }
                break;
            }

            case 'clearGeminiKey':
                await this.context.secrets.delete('antigravity.geminiKey');
                this.panel.webview.postMessage({ type: 'keyStatus', key: 'gemini', set: false });
                break;

            case 'setPicovoiceKey': {
                const key = msg.value as string;
                if (key) {
                    await this.context.secrets.store('antigravity.picovoiceKey', key);
                    this.panel.webview.postMessage({ type: 'keyStatus', key: 'picovoice', set: true });
                }
                break;
            }

            case 'clearPicovoiceKey':
                await this.context.secrets.delete('antigravity.picovoiceKey');
                this.panel.webview.postMessage({ type: 'keyStatus', key: 'picovoice', set: false });
                break;

            case 'testLocalTts': {
                const voice = config.get<string>('tts.localVoice', 'Samantha');
                const { execFile } = require('child_process');
                execFile('say', ['-v', voice, 'Hello! Antigravity voice is working.'], () => {});
                break;
            }
        }
    }

    private async sendCurrentSettings(): Promise<void> {
        const config = vscode.workspace.getConfiguration('antigravity');
        const geminiKey = await this.context.secrets.get('antigravity.geminiKey');
        const picovoiceKey = await this.context.secrets.get('antigravity.picovoiceKey');

        this.panel.webview.postMessage({
            type: 'settings',
            data: {
                triggerMode: config.get('triggerMode', 'tap'),
                wakeWordKeyword: config.get('wakeWord.keyword', 'computer'),
                wakeWordCustomPath: config.get('wakeWord.customKeywordPath', ''),
                ttsPreferredTier: config.get('tts.preferredTier', 'auto'),
                ttsLocalVoice: config.get('tts.localVoice', 'Samantha'),
                ttsGeminiVoice: config.get('tts.geminiVoice', 'Kore'),
                ttsLocalThreshold: config.get('tts.localThreshold', 200),
                whisperModel: config.get('whisper.model', 'base'),
                silenceDuration: config.get('silenceDuration', 10),
                geminiKeySet: !!geminiKey,
                picovoiceKeySet: !!picovoiceKey,
            },
        });
    }

    // ------------------------------------------------------------------
    // Webview HTML
    // ------------------------------------------------------------------

    private getHtml(): string {
        return /*html*/ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Antigravity Voice Settings</title>
<style>
    :root {
        --bg: var(--vscode-editor-background);
        --fg: var(--vscode-editor-foreground);
        --input-bg: var(--vscode-input-background);
        --input-border: var(--vscode-input-border);
        --input-fg: var(--vscode-input-foreground);
        --btn-bg: var(--vscode-button-background);
        --btn-fg: var(--vscode-button-foreground);
        --btn-hover: var(--vscode-button-hoverBackground);
        --section-border: var(--vscode-panel-border);
        --link: var(--vscode-textLink-foreground);
        --success: #4ec9b0;
        --warning: #cca700;
        --danger: #f14c4c;
    }
    body {
        font-family: var(--vscode-font-family);
        font-size: var(--vscode-font-size);
        color: var(--fg);
        background: var(--bg);
        padding: 20px 30px;
        max-width: 720px;
        margin: 0 auto;
    }
    h1 { font-size: 1.6em; margin-bottom: 4px; }
    h1 span { font-size: 0.5em; opacity: 0.6; font-weight: normal; }
    .subtitle { opacity: 0.7; margin-bottom: 24px; }
    section {
        border: 1px solid var(--section-border);
        border-radius: 6px;
        padding: 16px 20px;
        margin-bottom: 16px;
    }
    section h2 {
        font-size: 1.1em;
        margin: 0 0 12px 0;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .icon { font-size: 1.2em; }
    .field { margin-bottom: 14px; }
    .field:last-child { margin-bottom: 0; }
    label {
        display: block;
        font-weight: 600;
        margin-bottom: 4px;
    }
    .hint {
        font-size: 0.88em;
        opacity: 0.65;
        margin-top: 2px;
    }
    select, input[type="text"], input[type="number"], input[type="password"] {
        width: 100%;
        padding: 6px 10px;
        background: var(--input-bg);
        color: var(--input-fg);
        border: 1px solid var(--input-border);
        border-radius: 4px;
        font-size: inherit;
        box-sizing: border-box;
    }
    input[type="number"] { width: 100px; }
    .row { display: flex; gap: 10px; align-items: center; }
    button {
        padding: 6px 14px;
        background: var(--btn-bg);
        color: var(--btn-fg);
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: inherit;
    }
    button:hover { background: var(--btn-hover); }
    button.secondary {
        background: transparent;
        border: 1px solid var(--input-border);
        color: var(--fg);
    }
    button.danger { background: var(--danger); }
    .key-status {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 10px;
        font-size: 0.85em;
        font-weight: 600;
    }
    .key-status.set { background: var(--success); color: #000; }
    .key-status.unset { background: var(--warning); color: #000; }
    .conditional { display: none; }
    .conditional.visible { display: block; }
    .keybinding-display {
        font-family: monospace;
        background: var(--input-bg);
        border: 1px solid var(--input-border);
        padding: 8px 14px;
        border-radius: 4px;
        display: inline-block;
        font-size: 1.05em;
        letter-spacing: 1px;
    }
</style>
</head>
<body>

<h1>Antigravity Voice <span>v0.2.0</span></h1>
<p class="subtitle">Voice-driven AI coding assistant for VS Code</p>

<!-- ==================== TRIGGER MODE ==================== -->
<section>
    <h2><span class="icon">🎤</span> Trigger Mode</h2>
    <div class="field">
        <label for="triggerMode">Activation method</label>
        <select id="triggerMode" onchange="setSetting('triggerMode', this.value)">
            <option value="tap">Tap-to-Talk — press hotkey to start, press again to stop</option>
            <option value="wakeWord">Wake Word — always listening for a keyword</option>
        </select>
    </div>
    <div class="field">
        <label>Keyboard shortcut</label>
        <div class="row">
            <span class="keybinding-display" id="keybindingDisplay">Cmd+Alt+V</span>
            <button class="secondary" onclick="openKeybindings()">Customize</button>
        </div>
        <p class="hint">Opens VS Code keybinding editor filtered to Antigravity commands.</p>
    </div>
</section>

<!-- ==================== WAKE WORD ==================== -->
<section id="wakeWordSection" class="conditional">
    <h2><span class="icon">👂</span> Wake Word</h2>
    <div class="field">
        <label>Picovoice Access Key</label>
        <div class="row">
            <span class="key-status" id="picovoiceStatus">checking...</span>
            <button onclick="promptPicovoiceKey()">Set Key</button>
            <button class="secondary" onclick="clearPicovoiceKey()">Clear</button>
        </div>
        <p class="hint">Free at <a href="https://console.picovoice.ai/" style="color:var(--link)">console.picovoice.ai</a>. Required for wake-word detection.</p>
    </div>
    <div class="field">
        <label for="wakeWordKeyword">Built-in keyword</label>
        <select id="wakeWordKeyword" onchange="setSetting('wakeWord.keyword', this.value)">
            <option value="alexa">Alexa</option>
            <option value="americano">Americano</option>
            <option value="blueberry">Blueberry</option>
            <option value="bumblebee">Bumblebee</option>
            <option value="computer">Computer</option>
            <option value="grapefruit">Grapefruit</option>
            <option value="grasshopper">Grasshopper</option>
            <option value="hey barista">Hey Barista</option>
            <option value="hey google">Hey Google</option>
            <option value="hey siri">Hey Siri</option>
            <option value="jarvis">Jarvis</option>
            <option value="ok google">OK Google</option>
            <option value="picovoice">Picovoice</option>
            <option value="porcupine">Porcupine</option>
            <option value="terminator">Terminator</option>
        </select>
    </div>
    <div class="field">
        <label for="wakeWordCustomPath">Custom keyword file (.ppn)</label>
        <input type="text" id="wakeWordCustomPath" placeholder="/path/to/Hey-Antigravity_en_mac_v3_0_0.ppn"
               onchange="setSetting('wakeWord.customKeywordPath', this.value)">
        <p class="hint">Create custom keywords at <a href="https://console.picovoice.ai/" style="color:var(--link)">console.picovoice.ai</a>. Leave empty to use the built-in keyword above.</p>
    </div>
</section>

<!-- ==================== SPEECH-TO-TEXT ==================== -->
<section>
    <h2><span class="icon">🗣️</span> Speech-to-Text</h2>
    <div class="field">
        <label for="whisperModel">Whisper model</label>
        <select id="whisperModel" onchange="setSetting('whisper.model', this.value)">
            <option value="tiny">Tiny — fastest, least accurate (~39MB)</option>
            <option value="base">Base — good balance (~140MB)</option>
            <option value="small">Small — better accuracy (~460MB)</option>
            <option value="medium">Medium — best accuracy (~1.5GB)</option>
        </select>
    </div>
    <div class="field">
        <label for="silenceDuration">Silence timeout (seconds)</label>
        <input type="number" id="silenceDuration" min="2" max="30" value="10"
               onchange="setSetting('silenceDuration', Number(this.value))">
        <p class="hint">How long to wait after you stop speaking before finalizing.</p>
    </div>
</section>

<!-- ==================== TEXT-TO-SPEECH ==================== -->
<section>
    <h2><span class="icon">🔊</span> Text-to-Speech</h2>
    <div class="field">
        <label for="ttsPreferredTier">TTS engine</label>
        <select id="ttsPreferredTier" onchange="setSetting('tts.preferredTier', this.value); toggleTtsFields()">
            <option value="auto">Auto — short → local, long → Gemini</option>
            <option value="local">Always local (macOS say)</option>
            <option value="cloud">Always Gemini TTS</option>
        </select>
    </div>
    <div class="field">
        <label for="ttsLocalVoice">Local voice</label>
        <div class="row">
            <input type="text" id="ttsLocalVoice" value="Samantha" style="flex:1"
                   onchange="setSetting('tts.localVoice', this.value)">
            <button class="secondary" onclick="testLocalTts()">Test</button>
        </div>
        <p class="hint">Run <code>say -v ?</code> in Terminal to list available voices.</p>
    </div>
    <div class="field" id="geminiVoiceField">
        <label for="ttsGeminiVoice">Gemini voice</label>
        <select id="ttsGeminiVoice" onchange="setSetting('tts.geminiVoice', this.value)">
            <option value="Aoede">Aoede</option>
            <option value="Charon">Charon</option>
            <option value="Fenrir">Fenrir</option>
            <option value="Kore">Kore</option>
            <option value="Puck">Puck</option>
            <option value="Leda">Leda</option>
            <option value="Orus">Orus</option>
            <option value="Perseus">Perseus</option>
            <option value="Zephyr">Zephyr</option>
        </select>
    </div>
    <div class="field" id="thresholdField">
        <label for="ttsLocalThreshold">Auto-mode character threshold</label>
        <input type="number" id="ttsLocalThreshold" min="50" max="1000" value="200"
               onchange="setSetting('tts.localThreshold', Number(this.value))">
        <p class="hint">Responses shorter than this use local TTS in auto mode.</p>
    </div>
</section>

<!-- ==================== API KEYS ==================== -->
<section>
    <h2><span class="icon">🔑</span> API Keys</h2>
    <div class="field">
        <label>Gemini API Key</label>
        <div class="row">
            <span class="key-status" id="geminiStatus">checking...</span>
            <button onclick="promptGeminiKey()">Set Key</button>
            <button class="secondary" onclick="clearGeminiKey()">Clear</button>
        </div>
        <p class="hint">Required for AI processing and cloud TTS. Get one at <a href="https://aistudio.google.com/apikey" style="color:var(--link)">aistudio.google.com</a>.</p>
    </div>
</section>

<script>
const vscode = acquireVsCodeApi();

function setSetting(key, value) {
    vscode.postMessage({ type: 'setSetting', key, value });
}

function openKeybindings() {
    vscode.postMessage({ type: 'setSetting', key: '__openKeybindings', value: true });
}

function promptGeminiKey() {
    const key = prompt('Enter your Gemini API Key:');
    if (key) vscode.postMessage({ type: 'setGeminiKey', value: key });
}

function clearGeminiKey() {
    if (confirm('Remove the stored Gemini API key?'))
        vscode.postMessage({ type: 'clearGeminiKey' });
}

function promptPicovoiceKey() {
    const key = prompt('Enter your Picovoice Access Key (free at console.picovoice.ai):');
    if (key) vscode.postMessage({ type: 'setPicovoiceKey', value: key });
}

function clearPicovoiceKey() {
    if (confirm('Remove the stored Picovoice key?'))
        vscode.postMessage({ type: 'clearPicovoiceKey' });
}

function testLocalTts() {
    vscode.postMessage({ type: 'testLocalTts' });
}

function toggleWakeWordSection() {
    const mode = document.getElementById('triggerMode').value;
    const section = document.getElementById('wakeWordSection');
    section.classList.toggle('visible', mode === 'wakeWord');
}

function toggleTtsFields() {
    const tier = document.getElementById('ttsPreferredTier').value;
    document.getElementById('thresholdField').style.display = tier === 'auto' ? '' : 'none';
}

window.addEventListener('message', (event) => {
    const msg = event.data;
    if (msg.type === 'settings') {
        const d = msg.data;
        document.getElementById('triggerMode').value = d.triggerMode;
        document.getElementById('wakeWordKeyword').value = d.wakeWordKeyword;
        document.getElementById('wakeWordCustomPath').value = d.wakeWordCustomPath;
        document.getElementById('ttsPreferredTier').value = d.ttsPreferredTier;
        document.getElementById('ttsLocalVoice').value = d.ttsLocalVoice;
        document.getElementById('ttsGeminiVoice').value = d.ttsGeminiVoice;
        document.getElementById('ttsLocalThreshold').value = d.ttsLocalThreshold;
        document.getElementById('whisperModel').value = d.whisperModel;
        document.getElementById('silenceDuration').value = d.silenceDuration;

        updateKeyStatus('gemini', d.geminiKeySet);
        updateKeyStatus('picovoice', d.picovoiceKeySet);

        toggleWakeWordSection();
        toggleTtsFields();
    }
    if (msg.type === 'keyStatus') {
        updateKeyStatus(msg.key, msg.set);
    }
});

function updateKeyStatus(key, isSet) {
    const el = document.getElementById(key + 'Status');
    if (el) {
        el.textContent = isSet ? 'Configured' : 'Not set';
        el.className = 'key-status ' + (isSet ? 'set' : 'unset');
    }
}

document.getElementById('triggerMode').addEventListener('change', toggleWakeWordSection);

// Request current settings on load.
vscode.postMessage({ type: 'ready' });
</script>

</body>
</html>`;
    }
}
