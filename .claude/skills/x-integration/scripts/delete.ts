#!/usr/bin/env npx tsx
/**
 * X Integration - Delete Tweet
 * Usage: echo '{"tweetUrl":"https://x.com/user/status/123"}' | npx tsx delete.ts
 */

import { getBrowserContext, runScript, extractTweetId, ScriptResult } from '../lib/browser.js';
import { config } from '../lib/config.js';

interface DeleteInput {
  tweetUrl: string;
}

async function deleteTweet(input: DeleteInput): Promise<ScriptResult> {
  const { tweetUrl } = input;

  const tweetId = extractTweetId(tweetUrl);
  if (!tweetId) {
    return { success: false, message: 'Invalid tweet URL or ID.' };
  }

  let context = null;
  try {
    context = await getBrowserContext();
    const page = context.pages()[0] || await context.newPage();

    const url = `https://x.com/i/status/${tweetId}`;
    await page.goto(url, { timeout: config.timeouts.navigation, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(config.timeouts.pageLoad);

    // Open more options menu (···) on the first tweet article
    const moreButton = page.locator('article[data-testid="tweet"]').first()
      .locator('[data-testid="caret"]');
    await moreButton.waitFor({ timeout: config.timeouts.elementWait });
    await moreButton.click();
    await page.waitForTimeout(config.timeouts.afterClick);

    // Click Delete
    const deleteOption = page.locator('[data-testid="Dropdown"] [role="menuitem"]')
      .filter({ hasText: /delete/i });
    await deleteOption.waitFor({ timeout: config.timeouts.elementWait });
    await deleteOption.click();
    await page.waitForTimeout(config.timeouts.afterClick);

    // Confirm deletion in modal
    const confirmButton = page.locator('[data-testid="confirmationSheetConfirm"]');
    await confirmButton.waitFor({ timeout: config.timeouts.elementWait });
    await confirmButton.click();
    await page.waitForTimeout(config.timeouts.afterSubmit);

    return { success: true, message: `Tweet ${tweetId} deleted.` };

  } finally {
    if (context) await context.close();
  }
}

runScript<DeleteInput>(deleteTweet);
