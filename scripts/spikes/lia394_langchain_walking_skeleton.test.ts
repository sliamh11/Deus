import { beforeEach, describe, expect, it, vi } from 'vitest';

const executeBrokerToolMock = vi.fn();

// Mock only executeBrokerTool — getOpenAIToolDefinitions stays real so the
// tests exercise the actual tool-broker.ts tool list, unmodified.
vi.mock('../../container/agent-runner/src/tool-broker.js', async () => {
  const actual = await vi.importActual<
    typeof import('../../container/agent-runner/src/tool-broker.js')
  >('../../container/agent-runner/src/tool-broker.js');
  return {
    ...actual,
    executeBrokerTool: (...args: unknown[]) => executeBrokerToolMock(...args),
  };
});

const { getOpenAIToolDefinitions } =
  await import('../../container/agent-runner/src/tool-broker.js');
type ToolBrokerContext =
  import('../../container/agent-runner/src/tool-broker.js').ToolBrokerContext;
const { toolBrokerToLangChainTools, withHostAllowlist } =
  await import('./lia394_langchain_walking_skeleton.js');

function buildCtx(): ToolBrokerContext {
  return {
    cwd: '/tmp/lia394-test',
    containerInput: { groupFolder: 'test', chatJid: 'test' },
  };
}

describe('toolBrokerToLangChainTools', () => {
  beforeEach(() => {
    executeBrokerToolMock.mockReset();
  });

  it('maps every tool-broker definition to a StructuredTool with matching name/description', () => {
    const ctx = buildCtx();
    const tools = toolBrokerToLangChainTools(ctx);
    const definitions = getOpenAIToolDefinitions();

    expect(tools).toHaveLength(definitions.length);
    for (const def of definitions) {
      const match = tools.find((t) => t.name === def.name);
      expect(match, `expected a mapped tool named "${def.name}"`).toBeDefined();
      expect(match!.description).toBe(def.description);
    }
  });

  it("a mapped tool's execute function calls executeBrokerTool with the right name/args/ctx", async () => {
    executeBrokerToolMock.mockResolvedValue({
      query: 'langchain',
      results: [],
    });
    const ctx = buildCtx();
    const tools = toolBrokerToLangChainTools(ctx);
    const webSearch = tools.find((t) => t.name === 'web_search');
    expect(webSearch).toBeDefined();

    await webSearch!.invoke({ query: 'langchain' });

    expect(executeBrokerToolMock).toHaveBeenCalledTimes(1);
    expect(executeBrokerToolMock).toHaveBeenCalledWith(
      'web_search',
      { query: 'langchain' },
      ctx,
    );
  });
});

describe('withHostAllowlist', () => {
  beforeEach(() => {
    executeBrokerToolMock.mockReset();
  });

  it('rejects a disallowed hostname WITHOUT calling executeBrokerTool (negative check)', async () => {
    const ctx = buildCtx();
    const webFetch = toolBrokerToLangChainTools(ctx).find(
      (t) => t.name === 'web_fetch',
    );
    expect(webFetch).toBeDefined();
    const wrapped = withHostAllowlist(webFetch!, [
      'npmjs.com',
      'www.npmjs.com',
    ]);

    const result = await wrapped.invoke({ url: 'https://evil.example.com/x' });

    // The controlling assertion: the disallowed URL never reaches
    // executeBrokerTool — proves this is real enforcement, not a no-op.
    expect(executeBrokerToolMock).not.toHaveBeenCalled();

    const parsed = JSON.parse(result as string) as {
      ok: boolean;
      error: string;
    };
    expect(parsed.ok).toBe(false);
    expect(parsed.error).toMatch(/not in the allowed list/);
    expect(parsed.error).toMatch(/evil\.example\.com/);
  });

  it('allows https://npmjs.com and https://www.npmjs.com through to the real execute function', async () => {
    executeBrokerToolMock.mockResolvedValue({
      status: 200,
      url: 'https://npmjs.com/package/langchain',
      content: '<html>MIT</html>',
    });
    const ctx = buildCtx();
    const webFetch = toolBrokerToLangChainTools(ctx).find(
      (t) => t.name === 'web_fetch',
    );
    expect(webFetch).toBeDefined();
    const wrapped = withHostAllowlist(webFetch!, [
      'npmjs.com',
      'www.npmjs.com',
    ]);

    await wrapped.invoke({ url: 'https://npmjs.com/package/langchain' });
    expect(executeBrokerToolMock).toHaveBeenCalledWith(
      'web_fetch',
      { url: 'https://npmjs.com/package/langchain' },
      ctx,
    );

    executeBrokerToolMock.mockClear();
    executeBrokerToolMock.mockResolvedValue({
      status: 200,
      url: 'https://www.npmjs.com/package/langchain',
      content: '<html>MIT</html>',
    });

    await wrapped.invoke({ url: 'https://www.npmjs.com/package/langchain' });
    expect(executeBrokerToolMock).toHaveBeenCalledWith(
      'web_fetch',
      { url: 'https://www.npmjs.com/package/langchain' },
      ctx,
    );
  });

  it('rejects a malformed URL without throwing an unhandled exception', async () => {
    const ctx = buildCtx();
    const webFetch = toolBrokerToLangChainTools(ctx).find(
      (t) => t.name === 'web_fetch',
    );
    expect(webFetch).toBeDefined();
    const wrapped = withHostAllowlist(webFetch!, ['npmjs.com']);

    const result = await wrapped.invoke({ url: 'not-a-url-at-all' });

    expect(executeBrokerToolMock).not.toHaveBeenCalled();
    const parsed = JSON.parse(result as string) as {
      ok: boolean;
      error: string;
    };
    expect(parsed.ok).toBe(false);
    expect(parsed.error).toMatch(/malformed URL/);
  });
});
