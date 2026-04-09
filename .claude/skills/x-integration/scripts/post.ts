#!/usr/bin/env npx tsx
/**
 * X Integration - Post Tweet
 * Usage: echo '{"content":"Hello world"}' | npx tsx post.ts
 */

import { getBrowserContext, runScript, validateContent, config, ScriptResult } from '../lib/browser.js';

interface PostInput {
  content: string;
}

async function postTweet(input: PostInput): Promise<ScriptResult> {
  const { content } = input;

  const validationError = validateContent(content, 'Tweet');
  if (validationError) return validationError;

  let context = null;
  try {
    context = await getBrowserContext();
    const page = context.pages()[0] || await context.newPage();

    await page.goto('https://x.com/home', { timeout: config.timeouts.navigation, waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(config.timeouts.pageLoad);

    // Check if logged in
    const isLoggedIn = await page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').isVisible().catch(() => false);
    if (!isLoggedIn) {
      const onLoginPage = await page.locator('input[autocomplete="username"]').isVisible().catch(() => false);
      if (onLoginPage) {
        return { success: false, message: 'X login expired. Run /x-integration to re-authenticate.' };
      }
    }

    // Find and fill tweet input
    const tweetInput = page.locator('[data-testid="tweetTextarea_0"]');
    await tweetInput.waitFor({ timeout: config.timeouts.elementWait * 2 });
    await tweetInput.click();
    await page.waitForTimeout(config.timeouts.afterClick / 2);
    await tweetInput.fill(content);
    await page.waitForTimeout(config.timeouts.afterFill);

    // Click post button
    const postButton = page.locator('[data-testid="tweetButtonInline"]');
    await postButton.waitFor({ timeout: config.timeouts.elementWait });

    const isDisabled = await postButton.getAttribute('aria-disabled');
    if (isDisabled === 'true') {
      return { success: false, message: 'Post button disabled. Content may be empty or exceed character limit.' };
    }

    // Intercept CreateTweet API response to capture tweet ID
    const responsePromise = page.waitForResponse(
      (response) => response.url().includes('CreateTweet'),
      { timeout: 15000 }
    ).catch(() => null);

    await postButton.click();
    await page.waitForTimeout(config.timeouts.afterSubmit);

    let tweetUrl: string | undefined;
    try {
      const response = await responsePromise;
      if (response) {
        const data = await response.json().catch(() => null);
        const tweetId = data?.data?.create_tweet?.tweet_results?.result?.rest_id;
        if (tweetId) {
          const handleEl = page.locator('[data-testid="SideNav_AccountSwitcher_Button"] [dir="ltr"] span').first();
          const handleText = await handleEl.textContent().catch(() => null);
          const handle = handleText?.replace('@', '').trim() || 'me';
          tweetUrl = `https://x.com/${handle}/status/${tweetId}`;
        }
      }
    } catch {
      // URL capture failed silently — post still succeeded
    }

    return {
      success: true,
      message: tweetUrl
        ? `Tweet posted: ${tweetUrl}`
        : `Tweet posted: ${content.slice(0, 50)}${content.length > 50 ? '...' : ''}`,
      data: tweetUrl ? { url: tweetUrl } : undefined
    };

  } finally {
    if (context) await context.close();
  }
}

runScript<PostInput>(postTweet);
