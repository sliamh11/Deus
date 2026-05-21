import type {
  HookCallback,
  PostToolUseHookInput,
  PostToolUseFailureHookInput,
} from '@anthropic-ai/claude-agent-sdk';

export interface ToolCall {
  toolName: string;
  normalizedArgs: string;
  exitCode: number;
  succeeded: boolean;
}

export interface DoomLoopDetection {
  detected: boolean;
  repeatCount: number;
  message: string;
}

export function normalizeArgs(toolName: string, toolInput: unknown): string {
  if (toolName === 'Bash' || toolName === 'bash') {
    const input = toolInput as Record<string, unknown>;
    const cmd = typeof input?.command === 'string' ? input.command : '';
    return cmd.replace(/\s+/g, ' ').trim().toLowerCase().slice(0, 100);
  }
  if (
    toolName === 'Read' ||
    toolName === 'Write' ||
    toolName === 'Edit' ||
    toolName === 'read' ||
    toolName === 'write' ||
    toolName === 'edit'
  ) {
    const input = toolInput as Record<string, unknown>;
    return typeof input?.file_path === 'string' ? input.file_path : '';
  }
  return JSON.stringify(toolInput).slice(0, 100);
}

export class DoomLoopDetector {
  private streak: { key: string; count: number; warned: boolean } | null = null;

  constructor(private readonly threshold: number = 3) {}

  record(call: ToolCall): DoomLoopDetection {
    const key = `${call.toolName}:${call.normalizedArgs}`;

    if (call.succeeded) {
      this.streak = null;
      return { detected: false, repeatCount: 0, message: '' };
    }

    if (this.streak && this.streak.key === key) {
      this.streak.count += 1;
    } else {
      this.streak = { key, count: 1, warned: false };
    }

    if (this.streak.count >= this.threshold && !this.streak.warned) {
      this.streak.warned = true;
      const message = `[LOOP DETECTED] You've failed "${key}" ${this.streak.count} times consecutively. STOP. Do not retry. Read the error output, diagnose the root cause, and try a fundamentally different approach.`;
      return { detected: true, repeatCount: this.streak.count, message };
    }

    return { detected: false, repeatCount: this.streak.count, message: '' };
  }

  reset(): void {
    this.streak = null;
  }
}

export function createDoomLoopHook(detector: DoomLoopDetector): HookCallback {
  return async (input) => {
    const eventName = (input as { hook_event_name?: string }).hook_event_name;

    if (eventName === 'PostToolUseFailure') {
      const failInput = input as PostToolUseFailureHookInput;
      const detection = detector.record({
        toolName: failInput.tool_name,
        normalizedArgs: normalizeArgs(failInput.tool_name, failInput.tool_input),
        exitCode: 1,
        succeeded: false,
      });
      if (detection.detected) {
        return {
          hookSpecificOutput: {
            hookEventName: 'PostToolUseFailure' as const,
            additionalContext: detection.message,
          },
        };
      }
      return {};
    }

    const successInput = input as PostToolUseHookInput;
    const rawResponse = successInput.tool_response;
    let exitCode = 0;
    if (typeof rawResponse === 'string') {
      try {
        const parsed = JSON.parse(rawResponse) as Record<string, unknown>;
        exitCode = typeof parsed.exitCode === 'number' ? parsed.exitCode : 0;
      } catch {
        exitCode = 0;
      }
    } else if (rawResponse !== null && typeof rawResponse === 'object') {
      const resp = rawResponse as Record<string, unknown>;
      exitCode = typeof resp.exitCode === 'number' ? resp.exitCode : 0;
    }
    const succeeded = exitCode === 0;

    const detection = detector.record({
      toolName: successInput.tool_name,
      normalizedArgs: normalizeArgs(successInput.tool_name, successInput.tool_input),
      exitCode,
      succeeded,
    });

    if (detection.detected) {
      return {
        hookSpecificOutput: {
          hookEventName: 'PostToolUse' as const,
          additionalContext: detection.message,
        },
      };
    }
    return {};
  };
}
