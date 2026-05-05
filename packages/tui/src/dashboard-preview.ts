import { truncate } from './formatting.js';
import type { ChannelInfo } from './channel-status.js';
import type { DeusConfig } from './deus-config.js';
import type { ServiceStatus } from './service-status.js';
import type { WardensConfig } from './wardens-config.js';
import { WARDEN_TYPES, triggersLabel } from './wardens-config.js';

interface DashboardPreviewData {
  wardens: WardensConfig;
  services: ServiceStatus[];
  channels: ChannelInfo[];
  deusConfig: DeusConfig;
}

export function renderDashboardPreview(
  data: DashboardPreviewData,
  width = 72,
): string[] {
  const bodyWidth = Math.max(10, width - 4);
  const line = (text: string) =>
    `│ ${truncate(text, bodyWidth).padEnd(bodyWidth)} │`;
  const centered = (text: string) => {
    const clipped = truncate(text, bodyWidth);
    const pad = bodyWidth - clipped.length;
    const left = Math.floor(pad / 2);
    const right = pad - left;
    return `│ ${' '.repeat(left)}${clipped}${' '.repeat(right)} │`;
  };

  const wardens = Object.entries(data.wardens);
  const enabledWardens = wardens.filter(([, warden]) => warden.enabled).length;
  const runningServices = data.services.filter(
    (svc) => svc.status === 'running',
  ).length;
  const connectedChannels = data.channels.filter((ch) => ch.configured).length;
  const configKeys = Object.entries(data.deusConfig).filter(
    ([, value]) => value !== undefined && value !== null,
  ).length;

  const lines: string[] = [];
  lines.push('');
  lines.push(`╭${'─'.repeat(width - 2)}╮`);
  lines.push(centered('DEUS CONTROL CENTER'));
  lines.push(
    line(
      `Wardens ${enabledWardens}/${wardens.length} enabled · Services ${runningServices}/${data.services.length} running · Channels ${connectedChannels}/${data.channels.length} connected · Config ${configKeys} keys`,
    ),
  );
  lines.push(`├${'─'.repeat(width - 2)}┤`);

  lines.push(line('WARDENS'));
  for (const [name, warden] of wardens) {
    const icon = warden.enabled ? '✓' : '✗';
    const type = WARDEN_TYPES[name] ?? '';
    const triggers = triggersLabel(warden, name);
    lines.push(
      line(
        `  ${icon} ${truncate(name, 20).padEnd(20)} ${truncate(type, 20).padEnd(20)} ${truncate(triggers, 18)}`,
      ),
    );
  }

  lines.push(line('SERVICES'));
  for (const svc of data.services) {
    const icon =
      svc.status === 'running' ? '✓' : svc.status === 'stale' ? '~' : '✗';
    const detail = svc.detail ?? svc.status;
    lines.push(
      line(
        `  ${icon} ${truncate(svc.description, 28).padEnd(28)} ${truncate(detail, 18)}`,
      ),
    );
  }

  lines.push(line('CHANNELS'));
  for (const ch of data.channels) {
    const icon = ch.configured ? '✓' : '✗';
    const status = ch.configured ? 'connected' : 'not configured';
    lines.push(
      line(
        `  ${icon} ${truncate(ch.name, 16).padEnd(16)} ${truncate(status, 18)}`,
      ),
    );
  }

  lines.push(line('CONFIG'));
  for (const [key, value] of Object.entries(data.deusConfig)) {
    if (value === undefined || value === null) continue;
    lines.push(
      line(`  ${truncate(key, 20).padEnd(20)} ${truncate(String(value), 28)}`),
    );
  }
  lines.push(`╰${'─'.repeat(width - 2)}╯`);
  lines.push('');
  return lines;
}
