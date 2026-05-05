#!/usr/bin/env node
import React from 'react';
import { render } from 'ink';
import { App } from './app.js';
import { renderDashboardPreview } from './dashboard-preview.js';

if (!process.stdout.isTTY) {
  const { loadWardensConfig } = await import('./wardens-config.js');
  const { getServiceStatuses } = await import('./service-status.js');
  const { getChannelStatuses } = await import('./channel-status.js');
  const { loadDeusConfig } = await import('./deus-config.js');
  const preview = renderDashboardPreview(
    {
      wardens: loadWardensConfig(),
      services: getServiceStatuses(),
      channels: getChannelStatuses(),
      deusConfig: loadDeusConfig(),
    },
    80,
  );

  console.log(preview.join('\n'));
  process.exit(0);
}

render(<App />);
