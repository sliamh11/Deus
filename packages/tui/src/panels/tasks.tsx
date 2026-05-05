import React, { useState } from 'react';
import { Box, Text, useInput } from 'ink';
import { truncate } from '../formatting.js';
import { StatusIndicator } from '../components/status-indicator.js';
import { loadTasks, type TaskEntry } from '../task-data.js';

const STATUS_MAP: Record<TaskEntry['status'], 'on' | 'stale' | 'completed'> = {
  active: 'on',
  paused: 'stale',
  completed: 'completed',
};

const PROMPT_TRUNCATE_WIDTH = 52;
const SCHEDULE_TRUNCATE_WIDTH = 52;
const DETAIL_TRUNCATE_WIDTH = 56;
const ID_TRUNCATE_WIDTH = 24;

function scheduleLabel(task: TaskEntry): string {
  if (task.scheduleType === 'cron') return `cron: ${task.scheduleValue}`;
  if (task.scheduleType === 'interval') return `every ${task.scheduleValue}`;
  return `once: ${task.scheduleValue}`;
}

export function TasksPanel() {
  const [tasks] = useState<TaskEntry[]>(loadTasks);
  const [cursor, setCursor] = useState(0);

  const activeCount = tasks.filter((t) => t.status === 'active').length;
  const pausedCount = tasks.filter((t) => t.status === 'paused').length;

  useInput((_input, key) => {
    if (key.upArrow && cursor > 0) setCursor(cursor - 1);
    if (key.downArrow && cursor < tasks.length - 1) setCursor(cursor + 1);
  });

  if (tasks.length === 0) {
    return (
      <Box flexDirection="column">
        <Box marginBottom={1}>
          <Text color="cyan" bold>TASKS</Text>
          <Text dimColor> No scheduled tasks</Text>
        </Box>
        <Text dimColor>Schedule tasks via chat to see them here.</Text>
      </Box>
    );
  }

  const selected = tasks[cursor];
  const statusSummary = [
    `${activeCount}/${tasks.length} active`,
    pausedCount > 0 ? `${pausedCount} paused` : '',
  ].filter(Boolean).join(' · ');

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="cyan" bold>TASKS</Text>
        <Text dimColor> {statusSummary}</Text>
      </Box>

      <Box flexDirection="column">
        {tasks.map((task, i) => {
          const isSelected = i === cursor;
          return (
            <Box key={task.id}>
              <StatusIndicator status={STATUS_MAP[task.status]} label={truncate(task.prompt, PROMPT_TRUNCATE_WIDTH)} />
              <Text color={isSelected ? 'cyan' : undefined} bold={isSelected} dimColor={!isSelected}>
                {isSelected ? '▸ ' : '  '}
                {truncate(scheduleLabel(task), SCHEDULE_TRUNCATE_WIDTH)}
              </Text>
            </Box>
          );
        })}
      </Box>

      {selected && (
        <Box marginTop={1} borderStyle="round" borderColor="gray" paddingX={1} paddingY={0}>
          <Box flexDirection="column">
            <Box>
              <Text color="cyan" bold>{truncate(selected.id, ID_TRUNCATE_WIDTH)}</Text>
              <Text dimColor> {selected.groupFolder}</Text>
            </Box>
            <Text>
              <Text dimColor>Schedule: </Text>
              <Text>{truncate(scheduleLabel(selected), DETAIL_TRUNCATE_WIDTH)}</Text>
            </Text>
            <Text>
              <Text dimColor>Prompt: </Text>
              <Text>{truncate(selected.prompt, DETAIL_TRUNCATE_WIDTH)}</Text>
            </Text>
            {selected.nextRun && (
              <Text>
                <Text dimColor>Next run: </Text>
                <Text>{selected.nextRun}</Text>
              </Text>
            )}
            {selected.lastRun && (
              <Text>
                <Text dimColor>Last run: </Text>
                <Text>{selected.lastRun}</Text>
              </Text>
            )}
            {selected.lastResult && (
              <Text>
                <Text dimColor>Result: </Text>
                <Text>{truncate(selected.lastResult, DETAIL_TRUNCATE_WIDTH)}</Text>
              </Text>
            )}
          </Box>
        </Box>
      )}
    </Box>
  );
}
