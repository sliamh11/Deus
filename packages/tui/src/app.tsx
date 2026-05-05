import React, { useState } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import { TabBar } from './components/tab-bar.js';
import { WardensPanel } from './panels/wardens.js';
import { ServicesPanel } from './panels/services.js';
import { ChannelsPanel } from './panels/channels.js';
import { ConfigPanel } from './panels/config.js';
import { TasksPanel } from './panels/tasks.js';

const TABS = ['Wardens', 'Services', 'Channels', 'Config', 'Tasks'];
const TAB_DESCRIPTIONS = [
  'Toggle safety gates and inspect the selected warden.',
  'Check local service health and heartbeat freshness.',
  'See which channel adapters are configured.',
  'Review resolved Deus runtime configuration.',
  'View scheduled task status and details.',
];

export function App() {
  const { exit } = useApp();
  const [activeTab, setActiveTab] = useState(0);

  useInput((input, key) => {
    if (input === 'q' || input === 'Q') exit();
    if (key.leftArrow && activeTab > 0) setActiveTab(activeTab - 1);
    if (key.rightArrow && activeTab < TABS.length - 1) setActiveTab(activeTab + 1);
    if (input === '1') setActiveTab(0);
    if (input === '2') setActiveTab(1);
    if (input === '3') setActiveTab(2);
    if (input === '4') setActiveTab(3);
    if (input === '5') setActiveTab(4);
  });

  return (
    <Box flexDirection="column" paddingX={1} paddingY={1}>
      <Box marginTop={1}>
        <TabBar tabs={TABS} active={activeTab} />
      </Box>

      <Box marginTop={1} borderStyle="round" borderColor="gray" paddingX={1} paddingY={0} minHeight={14}>
        <Box flexDirection="column">
          {activeTab === 0 && <WardensPanel />}
          {activeTab === 1 && <ServicesPanel />}
          {activeTab === 2 && <ChannelsPanel />}
          {activeTab === 3 && <ConfigPanel />}
          {activeTab === 4 && <TasksPanel />}
        </Box>
      </Box>

      <Box marginTop={1} paddingX={1}>
        <Text dimColor>
          ←→ tabs  ↑↓ navigate  ⎵ toggle  1-5 jump  q quit · {TAB_DESCRIPTIONS[activeTab]}
        </Text>
      </Box>
    </Box>
  );
}
