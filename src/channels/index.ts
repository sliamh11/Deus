// Channel self-registration barrel file.
// All channel factories are imported unconditionally — each one checks for
// credentials and returns null if not configured, so unused channels are
// automatically disabled. This prevents git pulls from breaking active channels.

import './mcp-whatsapp.js';
import './mcp-telegram.js';
