export const ELLIPSIS = '…';

export function truncate(text: string, maxWidth: number): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (maxWidth <= 0) return '';
  if (normalized.length <= maxWidth) return normalized;
  if (maxWidth === 1) return ELLIPSIS;
  return `${normalized.slice(0, maxWidth - 1)}${ELLIPSIS}`;
}
