import React, { useState, useEffect } from 'react';
import { Box, Text } from 'ink';
import { truncate } from '../formatting.js';
import { loadDeusConfig, type DeusConfig } from '../deus-config.js';

export function ConfigPanel() {
  const [config, setConfig] = useState<DeusConfig>({});

  useEffect(() => {
    setConfig(loadDeusConfig());
  }, []);

  const entries = Object.entries(config).filter(([, v]) => v !== undefined && v !== null);

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="cyan" bold>
          CONFIG
        </Text>
        <Text dimColor>
          {' '}
          {entries.length} resolved values
        </Text>
      </Box>

      <Box flexDirection="column">
        {entries.map(([key, value]) => (
          <Box key={key}>
            <Text color="cyan" bold>
              {truncate(key, 24)}
            </Text>
            <Text>  {truncate(String(value), 42)}</Text>
          </Box>
        ))}
        {entries.length === 0 && <Text dimColor>No config found at ~/.config/deus/config.json</Text>}
      </Box>

      <Box marginTop={1}>
        <Text dimColor>Use /preferences or deus backend set to change settings.</Text>
      </Box>
    </Box>
  );
}
