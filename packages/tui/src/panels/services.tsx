import React, { useState, useEffect } from 'react';
import { Box, Text } from 'ink';
import { truncate } from '../formatting.js';
import { StatusIndicator } from '../components/status-indicator.js';
import { getServiceStatuses, type ServiceStatus } from '../service-status.js';

function statusBadge(status: ServiceStatus['status']): 'on' | 'off' | 'stale' | 'unknown' | 'unsupported' {
  if (status === 'running') return 'on';
  if (status === 'stale') return 'stale';
  if (status === 'unsupported') return 'unsupported';
  if (status === 'unknown') return 'unknown';
  return 'off';
}

export function ServicesPanel() {
  const [services, setServices] = useState<ServiceStatus[]>([]);

  useEffect(() => {
    setServices(getServiceStatuses());
  }, []);

  return (
    <Box flexDirection="column">
      <Box marginBottom={1}>
        <Text color="cyan" bold>
          SERVICES
        </Text>
        <Text dimColor>
          {' '}
          {services.filter((svc) => svc.status === 'running').length}/{services.length} running
        </Text>
      </Box>

      <Box flexDirection="column">
        {services.map((svc) => (
          <Box key={svc.label}>
            <StatusIndicator status={statusBadge(svc.status)} label={truncate(svc.description, 30)} />
            <Text dimColor>{truncate(svc.detail ?? svc.status, 28)}</Text>
          </Box>
        ))}
        {services.length === 0 && <Text dimColor>No services configured</Text>}
      </Box>

      <Box marginTop={1}>
        <Text dimColor>Heartbeat jobs stay visible even when the platform cannot inspect launchctl.</Text>
      </Box>
    </Box>
  );
}
