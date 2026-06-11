import fs from 'fs';
import path from 'path';
import sharp from 'sharp';
const MAX_DIMENSION = 1024;
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB
const IMAGE_REF_PATTERN = /\[Image: (attachments\/[^\]]+)\]/g;

export interface ProcessedImage {
  content: string;
  relativePath: string;
}

export interface ImageAttachment {
  relativePath: string;
  mediaType: string;
}

/** Channel-agnostic check for image content in a message. */
export function isImageMessage(msg: {
  message?: { imageMessage?: unknown };
}): boolean {
  return !!msg.message?.imageMessage;
}

export async function processImage(
  buffer: Buffer,
  groupDir: string,
  caption: string,
): Promise<ProcessedImage | null> {
  if (!buffer || buffer.length === 0 || buffer.length > MAX_FILE_SIZE)
    return null;

  const resized = await sharp(buffer)
    .resize(MAX_DIMENSION, MAX_DIMENSION, {
      fit: 'inside',
      withoutEnlargement: true,
    })
    .jpeg({ quality: 85 })
    .toBuffer();

  const attachDir = path.join(groupDir, 'attachments');
  fs.mkdirSync(attachDir, { recursive: true });

  const filename = `img-${Date.now()}-${Math.random().toString(36).slice(2, 6)}.jpg`;
  const filePath = path.join(attachDir, filename);
  fs.writeFileSync(filePath, resized);

  const relativePath = `attachments/${filename}`;
  const content = caption
    ? `[Image: ${relativePath}] ${caption}`
    : `[Image: ${relativePath}]`;

  return { content, relativePath };
}

/**
 * Host-side defense-in-depth: drop any `relativePath` that escapes the group's
 * `attachments/` subtree before it reaches the container (the regex capture
 * permits `..`, e.g. `attachments/../../proc/self/environ`). `processImage`
 * only ever emits `attachments/img-*.jpg`, so no legitimate path is rejected.
 * `path.posix` is deliberate — relativePaths are always POSIX container paths
 * regardless of host OS.
 */
function isWithinAttachments(relativePath: string): boolean {
  if (path.posix.isAbsolute(relativePath)) return false;
  const normalized = path.posix.normalize(relativePath);
  return normalized === 'attachments' || normalized.startsWith('attachments/');
}

export function parseImageReferences(
  messages: Array<{ content: string }>,
): ImageAttachment[] {
  const refs: ImageAttachment[] = [];
  for (const msg of messages) {
    let match: RegExpExecArray | null;
    IMAGE_REF_PATTERN.lastIndex = 0;
    while ((match = IMAGE_REF_PATTERN.exec(msg.content)) !== null) {
      const relativePath = match[1];
      if (!isWithinAttachments(relativePath)) continue;
      // Always JPEG — processImage() normalizes all images to .jpg
      refs.push({ relativePath, mediaType: 'image/jpeg' });
    }
  }
  return refs;
}
