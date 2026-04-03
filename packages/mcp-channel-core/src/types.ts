/** Normalized incoming message from any channel. */
export interface IncomingMessage {
  id: string;
  chat_id: string;
  sender: string;
  sender_name: string;
  content: string;
  timestamp: string;
  is_from_me?: boolean;
  is_group?: boolean;
  chat_name?: string;
  /** Channel-specific metadata (e.g., reply context, media info). */
  metadata?: Record<string, unknown>;
}

/** Connection status returned by get_status. */
export interface ChannelStatus {
  connected: boolean;
  channel: string;
  identity?: string;
  uptime_seconds?: number;
}

/** Chat/group info returned by list_chats. */
export interface ChatInfo {
  id: string;
  name: string;
  is_group: boolean;
}

/**
 * Interface that each channel implementation must provide.
 * The base server wraps this in MCP tools.
 */
export interface ChannelProvider {
  /** Channel name (e.g., 'whatsapp', 'telegram'). */
  readonly name: string;

  /** Connect to the messaging platform. */
  connect(): Promise<void>;

  /** Disconnect from the messaging platform. */
  disconnect(): Promise<void>;

  /** Whether the channel is currently connected. */
  isConnected(): boolean;

  /** Send a text message to a chat. */
  sendMessage(chatId: string, text: string): Promise<void>;

  /** Get current connection status. */
  getStatus(): ChannelStatus;

  /** Show/hide typing indicator. Optional — not all platforms support it. */
  setTyping?(chatId: string, isTyping: boolean): Promise<void>;

  /** List known chats/groups. Optional — not all platforms support listing. */
  listChats?(): Promise<ChatInfo[]>;

  /** Refresh group/chat metadata from the platform. */
  syncGroups?(): Promise<ChatInfo[]>;

  /**
   * Called by the base server to inject the message handler.
   * The channel calls this function whenever a new message arrives.
   */
  onMessage: (msg: IncomingMessage) => void;
}
