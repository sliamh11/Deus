/**
 * Step: register — Write channel registration config, create group folders.
 *
 * Accepts --channel to specify the messaging platform (whatsapp, telegram, slack, discord).
 * Uses parameterized SQL queries to prevent injection.
 */
import fs from 'fs';
import path from 'path';

import { STORE_DIR } from '../src/config.ts';
import { initDatabase, setRegisteredGroup } from '../src/db.ts';
import { isValidGroupFolder } from '../src/group-folder.ts';
import { logger } from '../src/logger.ts';
import { emitStatus } from './status.ts';

interface RegisterArgs {
  jid: string;
  name: string;
  trigger: string;
  folder: string;
  channel: string;
  requiresTrigger: boolean;
  isControlGroup: boolean;
  assistantName: string;
}

function parseArgs(args: string[]): RegisterArgs {
  const result: RegisterArgs = {
    jid: '',
    name: '',
    trigger: '',
    folder: '',
    channel: 'whatsapp', // backward-compat: pre-refactor installs omit --channel
    requiresTrigger: true,
    isControlGroup: false,
    assistantName: 'Deus',
  };

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--jid':
        result.jid = args[++i] || '';
        break;
      case '--name':
        result.name = args[++i] || '';
        break;
      case '--trigger':
        result.trigger = args[++i] || '';
        break;
      case '--folder':
        result.folder = args[++i] || '';
        break;
      case '--channel':
        result.channel = (args[++i] || '').toLowerCase();
        break;
      case '--no-trigger-required':
        result.requiresTrigger = false;
        break;
      case '--is-main':
        result.isControlGroup = true;
        break;
      case '--assistant-name':
        result.assistantName = args[++i] || 'Deus';
        break;
    }
  }

  return result;
}

/** Channel name to display format mapping */
const CHANNEL_FORMATS: Record<string, string> = {
  whatsapp: 'WhatsApp',
  telegram: 'Telegram',
  slack: 'Slack',
  discord: 'Discord',
};

/**
 * Generate a CLAUDE.md from a .template file, replacing placeholders.
 * Only writes if the target CLAUDE.md does not already exist (never overwrite customizations).
 */
function generateClaudeMdFromTemplate(
  templatePath: string,
  outputPath: string,
  vars: { assistantName: string; channelFormat?: string },
): boolean {
  if (fs.existsSync(outputPath)) {
    logger.info({ file: outputPath }, 'CLAUDE.md already exists, skipping generation');
    return false;
  }

  if (!fs.existsSync(templatePath)) {
    logger.warn({ file: templatePath }, 'Template file not found, skipping');
    return false;
  }

  let content = fs.readFileSync(templatePath, 'utf-8');
  content = content.replace(/\{\{ASSISTANT_NAME\}\}/g, vars.assistantName);
  if (vars.channelFormat) {
    content = content.replace(/\{\{CHANNEL_FORMAT\}\}/g, vars.channelFormat);
  }

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, content);
  logger.info({ file: outputPath }, 'Generated CLAUDE.md from template');
  return true;
}

export async function run(args: string[]): Promise<void> {
  const projectRoot = process.cwd();
  const parsed = parseArgs(args);

  if (!parsed.jid || !parsed.name || !parsed.trigger || !parsed.folder) {
    emitStatus('REGISTER_CHANNEL', {
      STATUS: 'failed',
      ERROR: 'missing_required_args',
      LOG: 'logs/setup.log',
    });
    process.exit(4);
  }

  if (!isValidGroupFolder(parsed.folder)) {
    emitStatus('REGISTER_CHANNEL', {
      STATUS: 'failed',
      ERROR: 'invalid_folder',
      LOG: 'logs/setup.log',
    });
    process.exit(4);
  }

  logger.info(parsed, 'Registering channel');

  // Ensure data and store directories exist (store/ may not exist on
  // fresh installs that skip WhatsApp auth, which normally creates it)
  fs.mkdirSync(path.join(projectRoot, 'data'), { recursive: true });
  fs.mkdirSync(STORE_DIR, { recursive: true });

  // Initialize database (creates schema + runs migrations)
  initDatabase();

  setRegisteredGroup(parsed.jid, {
    name: parsed.name,
    folder: parsed.folder,
    trigger: parsed.trigger,
    added_at: new Date().toISOString(),
    requiresTrigger: parsed.requiresTrigger,
    isControlGroup: parsed.isControlGroup,
  });

  logger.info('Wrote registration to SQLite');

  // Create group folders
  fs.mkdirSync(path.join(projectRoot, 'groups', parsed.folder, 'logs'), {
    recursive: true,
  });

  // Generate CLAUDE.md files from templates (never overwrites existing)
  const channelFormat = CHANNEL_FORMATS[parsed.channel] || parsed.channel;

  generateClaudeMdFromTemplate(
    path.join(projectRoot, 'groups', 'global', 'CLAUDE.md.template'),
    path.join(projectRoot, 'groups', 'global', 'CLAUDE.md'),
    { assistantName: parsed.assistantName },
  );

  // Use the main template for group-specific CLAUDE.md (channel-aware)
  generateClaudeMdFromTemplate(
    path.join(projectRoot, 'groups', 'main', 'CLAUDE.md.template'),
    path.join(projectRoot, 'groups', parsed.folder, 'CLAUDE.md'),
    { assistantName: parsed.assistantName, channelFormat },
  );

  // Update assistant name in .env if different from default
  let nameUpdated = false;
  if (parsed.assistantName !== 'Deus') {
    logger.info(
      { from: 'Deus', to: parsed.assistantName },
      'Updating assistant name',
    );

    // Update .env
    const envFile = path.join(projectRoot, '.env');
    if (fs.existsSync(envFile)) {
      let envContent = fs.readFileSync(envFile, 'utf-8');
      if (envContent.includes('ASSISTANT_NAME=')) {
        envContent = envContent.replace(
          /^ASSISTANT_NAME=.*$/m,
          `ASSISTANT_NAME="${parsed.assistantName}"`,
        );
      } else {
        envContent += `\nASSISTANT_NAME="${parsed.assistantName}"`;
      }
      fs.writeFileSync(envFile, envContent);
    } else {
      fs.writeFileSync(envFile, `ASSISTANT_NAME="${parsed.assistantName}"\n`);
    }
    logger.info('Set ASSISTANT_NAME in .env');
    nameUpdated = true;
  }

  emitStatus('REGISTER_CHANNEL', {
    JID: parsed.jid,
    NAME: parsed.name,
    FOLDER: parsed.folder,
    CHANNEL: parsed.channel,
    TRIGGER: parsed.trigger,
    REQUIRES_TRIGGER: parsed.requiresTrigger,
    ASSISTANT_NAME: parsed.assistantName,
    NAME_UPDATED: nameUpdated,
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}
