import { IncomingMessage } from './types.js';

const MAX_BUFFER_SIZE = 1000;

/**
 * Ring buffer for incoming messages.
 * Supports cursor-based pagination for the get_new_messages polling tool.
 */
export class MessageBuffer {
  private messages: IncomingMessage[] = [];
  private nextCursor = 0;

  push(msg: IncomingMessage): void {
    this.messages.push(msg);
    this.nextCursor++;

    // Trim old messages to prevent unbounded growth
    if (this.messages.length > MAX_BUFFER_SIZE) {
      const excess = this.messages.length - MAX_BUFFER_SIZE;
      this.messages.splice(0, excess);
    }
  }

  /**
   * Get messages since the given cursor.
   * Returns the messages and a new cursor for the next call.
   */
  getSince(cursor?: string): { messages: IncomingMessage[]; cursor: string } {
    const from = cursor ? parseInt(cursor, 10) : 0;

    // Calculate the offset into the current buffer
    const bufferStart = this.nextCursor - this.messages.length;
    const startIndex = Math.max(0, from - bufferStart);
    const newMessages = this.messages.slice(startIndex);

    return {
      messages: newMessages,
      cursor: String(this.nextCursor),
    };
  }

  get size(): number {
    return this.messages.length;
  }
}
