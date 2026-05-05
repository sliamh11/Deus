import React from 'react';
import { Text } from 'ink';

interface StatusIndicatorProps {
  status: 'on' | 'off' | 'stale' | 'unknown' | 'unsupported' | 'completed';
  label: string;
}

const COLORS: Record<string, string> = {
  on: 'green',
  off: 'red',
  stale: 'yellow',
  unknown: 'gray',
  unsupported: 'gray',
  completed: 'gray',
};

const ICONS: Record<string, string> = {
  on: '●',
  off: '○',
  stale: '◐',
  unknown: '?',
  unsupported: '—',
  completed: '✓',
};

export function StatusIndicator({ status, label }: StatusIndicatorProps) {
  return (
    <Text color={COLORS[status]} bold>
      {ICONS[status]} {label}
    </Text>
  );
}
