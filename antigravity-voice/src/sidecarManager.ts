import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { spawn, execFile, ChildProcessWithoutNullStreams } from 'child_process';

export type SidecarState = 'listening' | 'processing' | 'idle' | 'wakeword';
export type TranscriptionListener = (text: string) => void;
export type StateListener = (state: SidecarState) => void;
export type ErrorListener = (message: string) => void;
export type WakeWordListener = () => void;

interface SidecarMessage {
    type: 'ready' | 'status' | 'transcription' | 'error' | 'wakeword_detected';
    state?: SidecarState;
    text?: string;
    message?: string;
}

const MAX_RESTARTS = 3;
const READY_TIMEOUT_MS = 120_000; // allow extra time for first-run pip install + model download

/**
 * Owns the lifecycle of the Python voice_worker.py sidecar.
 *
 * On first activation the manager automatically creates a Python venv and
 * installs dependencies (the equivalent of running sidecar/setup.sh) so the
 * user never has to touch a terminal.
 *
 * Wire format is line-delimited JSON over stdio. The sidecar protocol is
 * documented in sidecar/voice_worker.py.
 */
export class SidecarManager implements vscode.Disposable {
    private process: ChildProcessWithoutNullStreams | undefined;
    private readonly output: vscode.OutputChannel;
    private readonly transcriptionListeners = new Set<TranscriptionListener>();
    private readonly stateListeners = new Set<StateListener>();
    private readonly errorListeners = new Set<ErrorListener>();
    private readonly wakeWordListeners = new Set<WakeWordListener>();

    private stdoutBuffer = '';
    private restartAttempts = 0;
    private disposed = false;
    private manuallyStopped = false;
    private readyPromise: Promise<void>;
    private resolveReady!: () => void;
    private rejectReady!: (err: Error) => void;
    private bootstrapDone = false;

    constructor(private readonly extensionPath: string) {
        this.output = vscode.window.createOutputChannel('Antigravity Voice');
        this.readyPromise = this.makeReadyPromise();
        // Kick off the async bootstrap → start chain.
        this.bootstrap();
    }

    private makeReadyPromise(): Promise<void> {
        return new Promise<void>((resolve, reject) => {
            this.resolveReady = resolve;
            this.rejectReady = reject;
        });
    }

    // ------------------------------------------------------------------
    // Auto-bootstrap: create venv + install deps on first launch
    // ------------------------------------------------------------------

    private async bootstrap(): Promise<void> {
        const sidecarDir = path.join(this.extensionPath, 'sidecar');
        const venvDir = path.join(sidecarDir, '.venv');
        const venvPython = path.join(venvDir, 'bin', 'python3');
        const reqFile = path.join(sidecarDir, 'requirements.txt');

        // If venv already exists and has the deps marker, skip setup.
        const markerFile = path.join(venvDir, '.deps-installed');
        if (fs.existsSync(venvPython) && fs.existsSync(markerFile)) {
            this.output.appendLine('[bootstrap] venv already set up — skipping');
            this.bootstrapDone = true;
            this.start();
            return;
        }

        // Show a progress notification so the user knows what's happening.
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'Antigravity Voice',
                cancellable: false,
            },
            async (progress) => {
                try {
                    // Step 1: Ensure python3 is available
                    progress.report({ message: 'Checking Python 3...' });
                    await this.exec('python3', ['--version']);

                    // Step 2: Create venv if missing
                    if (!fs.existsSync(venvPython)) {
                        progress.report({ message: 'Creating Python environment...' });
                        this.output.appendLine(`[bootstrap] creating venv at ${venvDir}`);
                        await this.exec('python3', ['-m', 'venv', venvDir]);
                    }

                    // Step 3: Upgrade pip
                    progress.report({ message: 'Upgrading pip...' });
                    await this.exec(venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip', '--quiet']);

                    // Step 4: Install requirements
                    progress.report({ message: 'Installing dependencies (this may take a few minutes)...' });
                    this.output.appendLine(`[bootstrap] pip install -r ${reqFile}`);
                    await this.exec(venvPython, ['-m', 'pip', 'install', '-r', reqFile]);

                    // Step 5: Write marker so we skip next time
                    fs.writeFileSync(markerFile, new Date().toISOString());
                    this.output.appendLine('[bootstrap] setup complete');

                    this.bootstrapDone = true;
                    this.start();
                } catch (err) {
                    const msg = `Auto-setup failed: ${err instanceof Error ? err.message : String(err)}. ` +
                        `You can try running sidecar/setup.sh manually.`;
                    this.output.appendLine(`[bootstrap] ${msg}`);
                    this.emitError(msg);
                    this.rejectReady(new Error(msg));
                }
            },
        );
    }

    /**
     * Run a command and return its stdout. Rejects on non-zero exit.
     */
    private exec(cmd: string, args: string[]): Promise<string> {
        return new Promise((resolve, reject) => {
            execFile(cmd, args, { maxBuffer: 10 * 1024 * 1024, timeout: 600_000 }, (err, stdout, stderr) => {
                if (stderr) {
                    this.output.append(`[exec] ${stderr}`);
                }
                if (err) {
                    reject(new Error(`${cmd} ${args.join(' ')} failed: ${err.message}`));
                } else {
                    resolve(stdout);
                }
            });
        });
    }

    private resolvePython(): string {
        const venvPython = path.join(this.extensionPath, 'sidecar', '.venv', 'bin', 'python3');
        if (fs.existsSync(venvPython)) {
            return venvPython;
        }
        return 'python3';
    }

    private start(): void {
        if (!this.bootstrapDone) {
            // bootstrap() will call start() when it finishes.
            return;
        }
        if (this.disposed) {
            return;
        }

        const scriptPath = path.join(this.extensionPath, 'sidecar', 'voice_worker.py');
        if (!fs.existsSync(scriptPath)) {
            const msg = `voice_worker.py not found at ${scriptPath}`;
            this.output.appendLine(`[error] ${msg}`);
            this.emitError(msg);
            this.rejectReady(new Error(msg));
            return;
        }

        const python = this.resolvePython();
        this.output.appendLine(`[info] spawning sidecar: ${python} ${scriptPath}`);

        let child: ChildProcessWithoutNullStreams;
        try {
            child = spawn(python, ['-u', scriptPath], {
                cwd: path.join(this.extensionPath, 'sidecar'),
                stdio: ['pipe', 'pipe', 'pipe'],
                env: { ...process.env },
            });
        } catch (err) {
            const msg = `Failed to spawn Python: ${err instanceof Error ? err.message : String(err)}. ` +
                `Ensure Python 3 is installed and run sidecar/setup.sh.`;
            this.output.appendLine(`[error] ${msg}`);
            this.emitError(msg);
            this.rejectReady(new Error(msg));
            return;
        }

        this.process = child;
        this.stdoutBuffer = '';

        child.stdout.on('data', (data: Buffer) => this.onStdout(data));
        child.stderr.on('data', (data: Buffer) => {
            this.output.append(`[python] ${data.toString()}`);
        });
        child.on('error', (err) => {
            const msg = `Sidecar process error: ${err.message}`;
            this.output.appendLine(`[error] ${msg}`);
            this.emitError(msg);
        });
        child.on('exit', (code, signal) => {
            this.output.appendLine(`[info] sidecar exited (code=${code}, signal=${signal})`);
            this.process = undefined;
            if (!this.disposed) {
                this.scheduleRestart();
            }
        });
    }

    private scheduleRestart(): void {
        if (this.manuallyStopped) {
            this.output.appendLine('[info] sidecar stopped manually — skipping auto-restart');
            return;
        }
        this.restartAttempts++;
        if (this.restartAttempts > MAX_RESTARTS) {
            const msg = `Sidecar crashed ${MAX_RESTARTS} times in a row. Giving up. ` +
                `Check the "Antigravity Voice" output channel and run sidecar/setup.sh.`;
            this.output.appendLine(`[error] ${msg}`);
            this.emitError(msg);
            this.rejectReady(new Error(msg));
            return;
        }
        // Reset the ready promise so the next listen() call waits for the new process.
        this.readyPromise = this.makeReadyPromise();
        const delay = Math.min(1000 * this.restartAttempts, 5000);
        this.output.appendLine(`[info] restarting sidecar in ${delay}ms (attempt ${this.restartAttempts}/${MAX_RESTARTS})`);
        setTimeout(() => this.start(), delay);
    }

    private onStdout(data: Buffer): void {
        this.stdoutBuffer += data.toString();
        let newlineIdx: number;
        while ((newlineIdx = this.stdoutBuffer.indexOf('\n')) !== -1) {
            const line = this.stdoutBuffer.slice(0, newlineIdx).trim();
            this.stdoutBuffer = this.stdoutBuffer.slice(newlineIdx + 1);
            if (line) {
                this.handleLine(line);
            }
        }
    }

    private handleLine(line: string): void {
        let msg: SidecarMessage;
        try {
            msg = JSON.parse(line) as SidecarMessage;
        } catch {
            this.output.appendLine(`[warn] non-JSON from sidecar: ${line}`);
            return;
        }
        switch (msg.type) {
            case 'ready':
                this.output.appendLine('[info] sidecar ready');
                this.restartAttempts = 0;
                this.resolveReady();
                break;
            case 'status':
                if (msg.state) {
                    this.stateListeners.forEach(cb => cb(msg.state!));
                }
                break;
            case 'transcription':
                this.transcriptionListeners.forEach(cb => cb(msg.text ?? ''));
                break;
            case 'wakeword_detected':
                this.output.appendLine('[info] wake word detected');
                this.wakeWordListeners.forEach(cb => cb());
                break;
            case 'error':
                this.output.appendLine(`[sidecar-error] ${msg.message}`);
                this.emitError(msg.message ?? 'Unknown sidecar error');
                break;
            default:
                this.output.appendLine(`[warn] unknown message type: ${line}`);
        }
    }

    private send(payload: object): boolean {
        if (!this.process || !this.process.stdin.writable) {
            return false;
        }
        this.process.stdin.write(JSON.stringify(payload) + '\n');
        return true;
    }

    private emitError(message: string): void {
        this.errorListeners.forEach(cb => cb(message));
    }

    async startListening(): Promise<boolean> {
        try {
            await Promise.race([
                this.readyPromise,
                new Promise<never>((_, reject) =>
                    setTimeout(() => reject(new Error('Timed out waiting for sidecar to become ready')), READY_TIMEOUT_MS)
                ),
            ]);
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            this.emitError(msg);
            return false;
        }
        const sent = this.send({ action: 'listen' });
        if (!sent) {
            this.emitError('Sidecar process is not running.');
        }
        return sent;
    }

    stopListening(): boolean {
        return this.send({ action: 'stop' });
    }

    onTranscription(listener: TranscriptionListener): vscode.Disposable {
        this.transcriptionListeners.add(listener);
        return new vscode.Disposable(() => this.transcriptionListeners.delete(listener));
    }

    onState(listener: StateListener): vscode.Disposable {
        this.stateListeners.add(listener);
        return new vscode.Disposable(() => this.stateListeners.delete(listener));
    }

    onError(listener: ErrorListener): vscode.Disposable {
        this.errorListeners.add(listener);
        return new vscode.Disposable(() => this.errorListeners.delete(listener));
    }

    onWakeWord(listener: WakeWordListener): vscode.Disposable {
        this.wakeWordListeners.add(listener);
        return new vscode.Disposable(() => this.wakeWordListeners.delete(listener));
    }

    // ------------------------------------------------------------------
    // Wake-word control
    // ------------------------------------------------------------------

    async startWakeWord(accessKey: string, keyword: string, customKeywordPath?: string): Promise<boolean> {
        try {
            await this.readyPromise;
        } catch {
            return false;
        }
        return this.send({
            action: 'start_wakeword',
            access_key: accessKey,
            keyword,
            custom_keyword_path: customKeywordPath ?? '',
        });
    }

    stopWakeWord(): boolean {
        return this.send({ action: 'stop_wakeword' });
    }

    // ------------------------------------------------------------------
    // Runtime configuration
    // ------------------------------------------------------------------

    configure(options: { whisper_model?: string; silence_duration?: number }): boolean {
        return this.send({ action: 'configure', ...options });
    }

    /** Whether the sidecar process is currently running. */
    get isRunning(): boolean {
        return this.process !== undefined && !this.process.killed;
    }

    /** Stop the sidecar and prevent auto-restart until ensureRunning() is called. */
    manualStop(): void {
        this.manuallyStopped = true;
        const proc = this.process;
        if (proc) {
            try {
                this.send({ action: 'shutdown' });
                proc.stdin.end();
            } catch {
                /* ignore */
            }
            setTimeout(() => {
                try {
                    if (!proc.killed) {
                        proc.kill();
                    }
                } catch {
                    /* ignore */
                }
            }, 500);
        }
        this.process = undefined;
    }

    /** Start the sidecar if it is not already running. Clears the manual-stop flag. */
    ensureRunning(): void {
        this.manuallyStopped = false;
        this.disposed = false;
        if (!this.isRunning) {
            this.restartAttempts = 0;
            this.readyPromise = this.makeReadyPromise();
            this.start();
        }
    }

    dispose(): void {
        this.disposed = true;
        const proc = this.process;
        if (proc) {
            try {
                this.send({ action: 'shutdown' });
                proc.stdin.end();
            } catch {
                /* ignore */
            }
            // If the process hasn't exited shortly after shutdown, force-kill so we
            // don't leave a zombie behind when VS Code unloads the extension.
            setTimeout(() => {
                try {
                    if (!proc.killed) {
                        proc.kill();
                    }
                } catch {
                    /* ignore */
                }
            }, 500);
        }
        this.output.dispose();
    }
}
