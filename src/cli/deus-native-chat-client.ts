/**
 * `deus chat` terminal client (LIA-428 / G1) — the compiled executable the
 * shell/PowerShell launchers invoke (`dist/cli/deus-native-chat-client.js`).
 *
 * Thin client: it reads the daemon's discovery record, sends authenticated
 * loopback requests, and renders ONLY normalized display events and terminal
 * prompts. It never instantiates a runtime, never reads provider
 * credentials, and never prints request/response objects, bearer tokens,
 * runtime event discriminants, session refs, or NDJSON framing.
 *
 * Non-goals (see deus-native-chat.ts's module doc): no G2 model-selection
 * flags, no G3 plan-mode toggle, no session picker. The only local commands
 * are /status, /exit, and /quit.
 */

import fs from 'fs';
import readline from 'readline';

import {
  nativeChatDiscoveryPath,
  parseDiscoveryRecord,
  NATIVE_CHAT_PROTOCOL_VERSION,
  type ChatDisplayEvent,
  type NativeChatDiscoveryRecord,
  type NativeChatStatus,
} from './deus-native-chat.js';

export const CHAT_UNAVAILABLE_MESSAGE =
  'Deus chat endpoint is unavailable: the Deus service is not running (or predates this CLI). ' +
  'Rebuild and restart it with `deus build`, then retry.';

/** Transport seam so the readline loop is testable without a live daemon. */
export interface ChatTransport {
  turn(
    prompt: string,
    cwd: string,
    onEvent: (event: ChatDisplayEvent) => void | Promise<void>,
  ): Promise<void>;
  status(): Promise<NativeChatStatus>;
  close(): Promise<void>;
}

const DISPLAY_KINDS = new Set([
  'assistant_text',
  'tool_use',
  'progress',
  'assistant_done',
  'chat_error',
]);

function isDisplayEvent(value: unknown): value is ChatDisplayEvent {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { kind?: unknown }).kind === 'string' &&
    DISPLAY_KINDS.has((value as { kind: string }).kind)
  );
}

export function createHttpChatTransport(
  record: NativeChatDiscoveryRecord,
): ChatTransport {
  const base = `http://${record.host}:${record.port}`;
  const authHeaders = { authorization: `Bearer ${record.token}` };

  return {
    async status(): Promise<NativeChatStatus> {
      const res = await fetch(`${base}/v1/native-chat/status`, {
        headers: authHeaders,
      });
      if (!res.ok) throw new Error(`status request failed (${res.status})`);
      return (await res.json()) as NativeChatStatus;
    },

    async close(): Promise<void> {
      const res = await fetch(`${base}/v1/native-chat/close`, {
        method: 'POST',
        headers: authHeaders,
      });
      if (!res.ok) throw new Error(`close request failed (${res.status})`);
    },

    async turn(prompt, cwd, onEvent): Promise<void> {
      const res = await fetch(`${base}/v1/native-chat/turn`, {
        method: 'POST',
        headers: { ...authHeaders, 'content-type': 'application/json' },
        body: JSON.stringify({
          version: NATIVE_CHAT_PROTOCOL_VERSION,
          prompt,
          cwd,
        }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`chat turn failed (${res.status})`);
      }
      // Parse the internal NDJSON stream; only validated display events
      // cross toward the renderer — framing never reaches the terminal.
      const decoder = new TextDecoder();
      let buffered = '';
      const consumeLine = async (line: string) => {
        const trimmed = line.trim();
        if (trimmed === '') return;
        let parsed: unknown;
        try {
          parsed = JSON.parse(trimmed);
        } catch {
          return; // Corrupt frame: drop, never render raw.
        }
        if (isDisplayEvent(parsed)) await onEvent(parsed);
      };
      for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
        buffered += decoder.decode(chunk, { stream: true });
        let newlineIndex = buffered.indexOf('\n');
        while (newlineIndex !== -1) {
          const line = buffered.slice(0, newlineIndex);
          buffered = buffered.slice(newlineIndex + 1);
          await consumeLine(line);
          newlineIndex = buffered.indexOf('\n');
        }
      }
      await consumeLine(buffered);
    },
  };
}

export interface ChatCliDeps {
  input: NodeJS.ReadableStream;
  output: NodeJS.WritableStream;
  errorOutput: NodeJS.WritableStream;
  transport: ChatTransport;
  cwd: string;
}

function renderStatus(
  status: NativeChatStatus,
  output: NodeJS.WritableStream,
): void {
  output.write(`Backend: ${status.backend}\n`);
  output.write(`Session: ${status.sessionId ?? 'not started'}\n`);
  output.write(`State:   ${status.state}\n`);
  output.write(`Output:  ${status.output}\n`);
}

/**
 * Interactive loop. Resolves with the process exit code once the user exits
 * (/exit, /quit, EOF/Ctrl-D, or SIGINT) or startup fails.
 */
export async function runChatCli(deps: ChatCliDeps): Promise<number> {
  const { input, output, errorOutput, transport, cwd } = deps;

  let startupStatus: NativeChatStatus;
  try {
    startupStatus = await transport.status();
  } catch {
    errorOutput.write(`${CHAT_UNAVAILABLE_MESSAGE}\n`);
    return 1;
  }

  output.write(
    'Deus chat — type a message. /status shows diagnostics, /exit quits.\n',
  );
  if (startupStatus.state === 'resumed') {
    output.write(
      'Resumed your previous conversation (run /status for details).\n',
    );
  }
  output.write('\n');

  const rl = readline.createInterface({ input, output, terminal: false });

  // Renderer state: whether assistant text is mid-line, so tool/progress
  // lines and turn completion insert clean separation.
  let lineOpen = false;
  const render = (event: ChatDisplayEvent): void => {
    switch (event.kind) {
      case 'assistant_text':
        output.write(event.text);
        lineOpen = !event.text.endsWith('\n');
        break;
      case 'tool_use':
        if (lineOpen) {
          output.write('\n');
          lineOpen = false;
        }
        output.write(`  ${event.label}\n`);
        break;
      case 'progress':
        if (lineOpen) {
          output.write('\n');
          lineOpen = false;
        }
        output.write(`  ${event.text}\n`);
        break;
      case 'assistant_done':
        if (lineOpen) output.write('\n');
        output.write('\n');
        lineOpen = false;
        break;
      case 'chat_error':
        if (lineOpen) {
          output.write('\n');
          lineOpen = false;
        }
        errorOutput.write(`Error: ${event.message}\n`);
        break;
      default: {
        const unhandled: never = event;
        void unhandled;
        break;
      }
    }
  };

  return await new Promise<number>((resolve) => {
    let finished = false;
    const queue: string[] = [];
    let processing = false;

    const finish = (code: number): void => {
      if (finished) return;
      finished = true;
      rl.close();
      // Best-effort authenticated close; the stored session must survive
      // regardless (the close route never clears it).
      void transport
        .close()
        .catch(() => {})
        .then(() => resolve(code));
    };

    const prompt = (): void => {
      if (!finished) output.write('> ');
    };

    const handleLine = async (raw: string): Promise<void> => {
      const line = raw.trim();
      if (line === '') return;
      if (line === '/exit' || line === '/quit') {
        finish(0);
        return;
      }
      if (line === '/status') {
        try {
          renderStatus(await transport.status(), output);
        } catch {
          errorOutput.write(`${CHAT_UNAVAILABLE_MESSAGE}\n`);
        }
        return;
      }
      try {
        await transport.turn(line, cwd, render);
      } catch {
        if (lineOpen) {
          output.write('\n');
          lineOpen = false;
        }
        errorOutput.write(
          'Error: the chat request failed. Is the Deus service still running?\n',
        );
      }
    };

    // Sequential pump: lines received while a turn is in flight are queued,
    // never sent as overlapping requests.
    let eofSeen = false;
    const pump = async (): Promise<void> => {
      if (processing) return;
      processing = true;
      while (queue.length > 0 && !finished) {
        const line = queue.shift() as string;
        await handleLine(line);
      }
      processing = false;
      if (eofSeen && queue.length === 0) {
        finish(0);
        return;
      }
      prompt();
    };

    rl.on('line', (line) => {
      queue.push(line);
      void pump();
    });
    // EOF (Ctrl-D) or input stream end: drain any queued lines, then exit
    // cleanly. (finish() itself calls rl.close(); the finished flag makes
    // the resulting re-entrant 'close' a no-op.)
    rl.on('close', () => {
      eofSeen = true;
      if (!processing && queue.length === 0) finish(0);
    });
    rl.on('SIGINT', () => finish(0));

    prompt();
  });
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.length > 0) {
    // LIA-428 has no flags; reject rather than silently ignore (G2/G3 add
    // their options through DeusNativeChatOptions later).
    process.stderr.write('deus chat takes no arguments.\n');
    process.exit(2);
  }

  let record: NativeChatDiscoveryRecord | undefined;
  try {
    record = parseDiscoveryRecord(
      fs.readFileSync(nativeChatDiscoveryPath(), 'utf8'),
    );
  } catch {
    record = undefined;
  }
  if (!record) {
    process.stderr.write(`${CHAT_UNAVAILABLE_MESSAGE}\n`);
    process.exit(1);
  }

  const transport = createHttpChatTransport(record);
  const sigint = () => {
    // readline (terminal:false) does not translate SIGINT for us; exit
    // cleanly with a best-effort close, leaving the stored session intact.
    void transport
      .close()
      .catch(() => {})
      .then(() => process.exit(0));
  };
  process.once('SIGINT', sigint);

  const code = await runChatCli({
    input: process.stdin,
    output: process.stdout,
    errorOutput: process.stderr,
    transport,
    cwd: process.cwd(),
  });
  process.exit(code);
}

const isDirectRun =
  process.argv[1]?.endsWith('deus-native-chat-client.js') ||
  process.argv[1]?.endsWith('deus-native-chat-client.ts');
if (isDirectRun) {
  void main();
}
