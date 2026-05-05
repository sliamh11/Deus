import React, { useState } from 'react';
import { Box, Text, useInput } from 'ink';
import { truncate } from '../formatting.js';
import { StatusIndicator } from '../components/status-indicator.js';
import {
  loadWardensConfig,
  saveWardensConfig,
  WARDEN_DESCRIPTIONS,
  WARDEN_TYPES,
  BLOCKING_WARDENS,
  triggersLabel,
  type WardensConfig,
} from '../wardens-config.js';

export function WardensPanel() {
  const [config, setConfig] = useState<WardensConfig>(loadWardensConfig);
  const [cursor, setCursor] = useState(0);
  const [warning, setWarning] = useState('');

  const names = Object.keys(config);
  const enabledCount = names.filter((name) => config[name]?.enabled).length;
  const blockingActive = names.filter((name) => config[name]?.enabled && BLOCKING_WARDENS.has(name)).length;

  useInput((input, key) => {
    if (key.upArrow && cursor > 0) setCursor(cursor - 1);
    if (key.downArrow && cursor < names.length - 1) setCursor(cursor + 1);
    if (input === ' ' || key.return) {
      const name = names[cursor]!;
      const warden = config[name]!;
      const newEnabled = !warden.enabled;

      if (!newEnabled && BLOCKING_WARDENS.has(name)) {
        setWarning(`⚠ Disabling ${name} removes a safety gate`);
        setTimeout(() => setWarning(''), 3000);
      }

      const updated = { ...config, [name]: { ...warden, enabled: newEnabled } };
      saveWardensConfig(updated);
      setConfig(updated);
    }
  });

  const selectedName = names[cursor] ?? '';
  const selected = config[selectedName];

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="cyan" bold>
          WARDENS
        </Text>
        <Text dimColor>
          {' '}
          {enabledCount}/{names.length} enabled · {blockingActive} blocking active
        </Text>
      </Box>

      <Box flexDirection="column">
        {names.map((name, i) => {
          const warden = config[name]!;
          const isSelected = i === cursor;
          const description = WARDEN_DESCRIPTIONS[name] ?? 'No description available';
          return (
            <Box key={name}>
              <StatusIndicator status={warden.enabled ? 'on' : 'off'} label={truncate(name, 24)} />
              <Text color={isSelected ? 'cyan' : undefined} bold={isSelected} dimColor={!isSelected}>
                {isSelected ? '▸ ' : '  '}
                {truncate(description, 42)}
              </Text>
            </Box>
          );
        })}
      </Box>

      {warning && (
        <Box marginTop={1} borderStyle="round" borderColor="yellow" paddingX={1}>
          <Text color="yellow">{warning}</Text>
        </Box>
      )}

      {selected && (
        <Box marginTop={1} borderStyle="round" borderColor="gray" paddingX={1} paddingY={0}>
          <Box flexDirection="column">
            <Box>
              <Text color="cyan" bold>
                {truncate(selectedName, 24)}
              </Text>
              <Text dimColor>
                {' '}
                {truncate(WARDEN_TYPES[selectedName] ?? 'Unknown', 28)}
              </Text>
            </Box>
            <Text>
              <Text dimColor>Triggers: </Text>
              <Text>{truncate(triggersLabel(selected, selectedName), 56)}</Text>
            </Text>
            {selected.custom_instructions && (
              <Text>
                <Text dimColor>Instructions: </Text>
                <Text>
                  {truncate(selected.custom_instructions, 56)}
                </Text>
              </Text>
            )}
          </Box>
        </Box>
      )}
    </Box>
  );
}
