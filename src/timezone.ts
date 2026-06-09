/**
 * Convert a UTC ISO timestamp to a localized display string.
 * Uses the Intl API (no external dependencies).
 */
export function formatLocalTime(utcIso: string, timezone: string): string {
  const date = new Date(utcIso);
  return date.toLocaleString('en-US', {
    timeZone: timezone,
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

// Render a UTC ISO timestamp in the machine's local timezone for display;
// storage stays UTC. `tz` is injectable so tests are deterministic across zones.

/** Machine-local timezone, derived once via the Intl API (no hardcoded offset). */
const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

function intlParts(utcIso: string, tz: string): Record<string, string> {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date(utcIso));
  const map: Record<string, string> = {};
  for (const p of parts) map[p.type] = p.value;
  // Intl can emit '24' for midnight in some runtimes; normalise to '00'.
  if (map.hour === '24') map.hour = '00';
  return map;
}

/** Render a UTC ISO string as local `HH:MM` (24-hour). */
export function formatLocalHHMM(utcIso: string, tz: string = LOCAL_TZ): string {
  const p = intlParts(utcIso, tz);
  return `${p.hour}:${p.minute}`;
}

/** Render a UTC ISO string as local `YYYY-MM-DD HH:MM` (24-hour). */
export function formatLocalDateTime(
  utcIso: string,
  tz: string = LOCAL_TZ,
): string {
  const p = intlParts(utcIso, tz);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}
