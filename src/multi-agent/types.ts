export type SubagentStatus = 'DONE' | 'DONE_WITH_CONCERNS' | 'BLOCKED';

export interface SubagentResult {
  status: SubagentStatus;
  output: string;
  concerns?: string[];
  blockedReason?: string;
}

export interface OrchestratorResult {
  status: 'success' | 'partial' | 'error';
  results: SubagentResult[];
  concerns: string[];
}

export interface SubagentTask {
  id: string;
  role: string;
  goal: string;
  backstory: string;
  prompt: string;
  mode: 'read' | 'write';
  contextFrom?: string[];
}
