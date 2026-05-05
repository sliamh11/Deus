import React from 'react';
import { Box, Text } from 'ink';

interface TabBarProps {
  tabs: string[];
  active: number;
}

export function TabBar({ tabs, active }: TabBarProps) {
  return (
    <Box flexDirection="row" flexWrap="wrap">
      {tabs.map((tab, i) => (
        <Box key={tab} marginRight={1} marginBottom={1}>
          <Text
            bold={i === active}
            color={i === active ? 'black' : 'gray'}
            backgroundColor={i === active ? 'cyan' : undefined}
          >
            {` ${i + 1}. ${tab} `}
          </Text>
        </Box>
      ))}
    </Box>
  );
}
