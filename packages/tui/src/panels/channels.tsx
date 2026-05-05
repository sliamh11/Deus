import React, { useState, useEffect } from 'react';
import { Box, Text } from 'ink';
import { truncate } from '../formatting.js';
import { StatusIndicator } from '../components/status-indicator.js';
import { getChannelStatuses, type ChannelInfo } from '../channel-status.js';

export function ChannelsPanel() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);

  useEffect(() => {
    setChannels(getChannelStatuses());
  }, []);

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="cyan" bold>
          CHANNELS
        </Text>
        <Text dimColor>
          {' '}
          {channels.filter((ch) => ch.configured).length}/{channels.length} configured
        </Text>
      </Box>

      <Box flexDirection="column">
        {channels.map((ch) => (
          <Box key={ch.name}>
            <StatusIndicator status={ch.configured ? 'on' : 'off'} label={truncate(ch.name, 18)} />
            <Text dimColor>{ch.configured ? 'configured' : 'not configured'}</Text>
          </Box>
        ))}
      </Box>

      <Box marginTop={1}>
        <Text dimColor>Use /add-whatsapp, /add-telegram, and friends to bring channels online.</Text>
      </Box>
    </Box>
  );
}
