/**
 * Shared types and constants for log viewers
 */

export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'ALERT'] as const;
export const AUDIENCES = ['user', 'developer', 'agent'] as const;

export type LogLevel = typeof LOG_LEVELS[number];
export type Audience = typeof AUDIENCES[number];
export type LogMode = 'build' | 'test' | 'agent';
export type TimeMode = 'delta' | 'wall';
export type SourceMode = 'source' | 'logger';
export type ConnectionState = 'disconnected' | 'connecting' | 'connected';

// Short level names for compact display
export const LEVEL_SHORT: Record<LogLevel, string> = {
  DEBUG: 'D',
  INFO: 'I',
  WARNING: 'W',
  ERROR: 'E',
  ALERT: 'A',
};

// Catppuccin-inspired colors for source files
export const SOURCE_COLORS = [
  '#cba6f7', // mauve
  '#f38ba8', // red
  '#fab387', // peach
  '#f9e2af', // yellow
  '#a6e3a1', // green
  '#94e2d5', // teal
  '#89dceb', // sky
  '#74c7ec', // sapphire
  '#89b4fa', // blue
  '#b4befe', // lavender
  '#f5c2e7', // pink
  '#eba0ac', // maroon
];

// Tooltips for UI elements
export const TOOLTIPS = {
  timestamp: 'Click: toggle format',
  level: 'Click: toggle short/full',
  source: 'Source location',
  logger: 'Logger module',
  stage: 'Build stage',
  test: 'Test name',
  message: 'Log message',
  search: 'Filter messages',
  autoScroll: 'Auto-scroll logs',
};

// --- Log Entry Types ---

export interface BaseLogEntry {
  timestamp: string;
  level: LogLevel;
  audience: Audience;
  logger_name: string;
  message: string;
  stage?: string | null;
  source_file?: string | null;
  source_line?: number | null;
  ato_traceback?: string | null;
  python_traceback?: string | null;
  objects?: unknown;
}

export interface BuildLogEntry extends BaseLogEntry {}

export interface TestLogEntry extends BaseLogEntry {
  test_name?: string | null;
}

// The agent's own run log (planning / tool calls / errors), a separate source
// from build/test logs. Mapped onto the shared entry shape server-side so the
// existing LogDisplay renders it; the extra fields are kept for detail/grouping.
export interface AgentLogEntry extends BaseLogEntry {
  event?: string | null;
  phase?: string | null;
  tool_name?: string | null;
  run_id?: string | null;
}

export type LogEntry = BuildLogEntry | TestLogEntry | AgentLogEntry;

// Streaming entries include id for cursor tracking
export interface StreamLogEntry extends BaseLogEntry {
  id: number;
}

export interface TestStreamLogEntry extends TestLogEntry {
  id: number;
}

export interface AgentStreamLogEntry extends AgentLogEntry {
  id: number;
}

// --- WebSocket Message Types ---

export interface BuildLogResult {
  type: 'logs_result';
  logs: BuildLogEntry[];
}

export interface TestLogResult {
  type: 'test_logs_result';
  logs: TestLogEntry[];
}

export interface StreamResult {
  type: 'logs_stream';
  logs: StreamLogEntry[];
  last_id: number;
}

export interface TestStreamResult {
  type: 'test_logs_stream';
  logs: TestStreamLogEntry[];
  last_id: number;
}

export interface AgentLogResult {
  type: 'agent_logs_result';
  logs: AgentStreamLogEntry[];
  session_id: string | null;
}

export interface AgentStreamResult {
  type: 'agent_logs_stream';
  logs: AgentStreamLogEntry[];
  last_id: number;
  session_id: string | null;
}

export interface LogError {
  type: 'logs_error';
  error: string;
}

export type LogResult =
  | BuildLogResult
  | TestLogResult
  | StreamResult
  | TestStreamResult
  | AgentLogResult
  | AgentStreamResult
  | LogError;

// --- Tree Types ---

export interface TreeNode {
  entry: LogEntry;
  depth: number;
  content: string;
  children: TreeNode[];
}

export interface LogTreeGroup {
  type: 'standalone' | 'tree';
  root: TreeNode;
}

// --- Request Payload Types ---

export interface BuildLogRequest {
  build_id: string;
  stage?: string | null;
  log_levels?: LogLevel[] | null;
  audience: Audience;
  after_id?: number;
  count?: number;
  subscribe?: boolean;
}

export interface TestLogRequest {
  test_run_id: string;
  test_name?: string | null;
  log_levels?: LogLevel[] | null;
  audience: Audience;
  after_id?: number;
  count?: number;
  subscribe?: boolean;
}

export interface AgentLogRequest {
  agent: true;
  // Optional: omit to follow the most recent agent session.
  agent_session_id?: string | null;
  run_id?: string | null;
  log_levels?: LogLevel[] | null;
  after_id?: number;
  count?: number;
  subscribe?: boolean;
}
