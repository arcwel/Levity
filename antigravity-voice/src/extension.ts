import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { VoiceProvider } from './voiceProvider';
import { SidecarManager } from './sidecarManager';
import { SettingsPanel } from './settingsPanel';

let sidecarManager: SidecarManager | undefined;

export function activate(context: vscode.ExtensionContext) {
    // Boot the Python sidecar eagerly so Whisper is preloaded before the user
    // issues their first voice command.
    sidecarManager = new SidecarManager(context.extensionPath);

    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    context.subscriptions.push(statusBar);

    const voiceProvider = new VoiceProvider(context, sidecarManager, statusBar);
    context.subscriptions.push({ dispose: () => voiceProvider.dispose() });

    // ---- Commands ----

    const startVoiceCommand = vscode.commands.registerCommand('antigravity.voice.start', async () => {
        await voiceProvider.triggerVoiceWorkflow();
    });

    const stopVoiceCommand = vscode.commands.registerCommand('antigravity.voice.stop', () => {
        voiceProvider.stopListening();
    });

    const setKeyCommand = vscode.commands.registerCommand('antigravity.voice.setGeminiKey', async () => {
        const key = await vscode.window.showInputBox({
            prompt: 'Enter your Gemini API Key',
            password: true,
            ignoreFocusOut: true,
        });
        if (key) {
            await context.secrets.store('antigravity.geminiKey', key);
            vscode.window.showInformationMessage('Gemini API Key saved securely.');
        }
    });

    const setPicovoiceKeyCommand = vscode.commands.registerCommand('antigravity.voice.setPicovoiceKey', async () => {
        const key = await vscode.window.showInputBox({
            prompt: 'Enter your Picovoice Access Key (free at console.picovoice.ai)',
            password: true,
            ignoreFocusOut: true,
        });
        if (key) {
            await context.secrets.store('antigravity.picovoiceKey', key);
            vscode.window.showInformationMessage('Picovoice Access Key saved securely.');
            // Re-apply trigger mode in case they just enabled wake-word.
            voiceProvider.applyTriggerMode().catch((e) => {
                vscode.window.showErrorMessage(`Antigravity: ${e instanceof Error ? e.message : String(e)}`);
            });
        }
    });

    const openSettingsCommand = vscode.commands.registerCommand('antigravity.voice.openSettings', () => {
        SettingsPanel.createOrShow(context);
    });

    const reinstallCommand = vscode.commands.registerCommand('antigravity.voice.reinstallDeps', async () => {
        const markerFile = path.join(context.extensionPath, 'sidecar', '.venv', '.deps-installed');
        try {
            fs.unlinkSync(markerFile);
        } catch {
            // marker may not exist yet
        }
        vscode.window.showInformationMessage(
            'Antigravity: Dependency marker cleared. Reload the window to re-install.',
            'Reload Now',
        ).then(choice => {
            if (choice === 'Reload Now') {
                vscode.commands.executeCommand('workbench.action.reloadWindow');
            }
        });
    });

    // ---- Server start/stop commands ----

    const startServerCommand = vscode.commands.registerCommand('antigravity.server.start', () => {
        sidecarManager!.ensureRunning();
        voiceProvider.setServerStopped(false);
        voiceProvider.applySettings();
        vscode.window.showInformationMessage('Antigravity: Voice server started.');
    });

    const stopServerCommand = vscode.commands.registerCommand('antigravity.server.stop', () => {
        sidecarManager!.manualStop();
        voiceProvider.setServerStopped(true);
        voiceProvider.applySettings();
        vscode.window.showInformationMessage('Antigravity: Voice server stopped.');
    });

    // ---- Wake-word toggle ----

    const wakeWordToggleCommand = vscode.commands.registerCommand('antigravity.wakeword.toggle', async () => {
        const config = vscode.workspace.getConfiguration('antigravity');
        const currentMode = config.get<string>('triggerMode', 'tap');

        if (currentMode === 'wakeWord') {
            // Turn wake word off → switch to tap mode.
            await config.update('triggerMode', 'tap', vscode.ConfigurationTarget.Global);
            vscode.window.showInformationMessage('Antigravity: Wake word OFF (switched to tap mode).');
        } else {
            // Turn wake word on.
            const accessKey = await context.secrets.get('antigravity.picovoiceKey');
            if (!accessKey) {
                vscode.window.showWarningMessage(
                    'Antigravity: Wake-word mode requires a Picovoice access key. ' +
                    'Run "Antigravity: Set Picovoice Access Key" first.',
                );
                return;
            }
            await config.update('triggerMode', 'wakeWord', vscode.ConfigurationTarget.Global);
            vscode.window.showInformationMessage('Antigravity: Wake word ON.');
        }
    });

    // ---- Voice response toggle ----

    const responseToggleCommand = vscode.commands.registerCommand('antigravity.response.toggle', () => {
        const current = voiceProvider.getResponseEnabled();
        voiceProvider.setResponseEnabled(!current);
        // Refresh status bar to show/hide (muted).
        voiceProvider.applySettings();
        vscode.window.showInformationMessage(
            `Antigravity: Voice response ${!current ? 'ON' : 'OFF (muted)'}.`,
        );
    });

    context.subscriptions.push(
        startVoiceCommand,
        stopVoiceCommand,
        setKeyCommand,
        setPicovoiceKeyCommand,
        openSettingsCommand,
        reinstallCommand,
        startServerCommand,
        stopServerCommand,
        wakeWordToggleCommand,
        responseToggleCommand,
    );

    // ---- React to configuration changes ----

    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('antigravity')) {
                voiceProvider.applySettings();
            }
        }),
    );

    // ---- Handle the "open keybindings" pseudo-setting from the webview ----

    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            // The settings panel sends a fake config key to trigger this.
            if (e.affectsConfiguration('antigravity.__openKeybindings')) {
                vscode.commands.executeCommand(
                    'workbench.action.openGlobalKeybindings',
                    'antigravity.voice',
                );
            }
        }),
    );

    // Apply initial settings (trigger mode, config sync to sidecar).
    voiceProvider.applySettings();
}

export function deactivate() {
    sidecarManager?.dispose();
    sidecarManager = undefined;
}
