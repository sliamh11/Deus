#!/usr/bin/env node
/**
 * CLI for solution atom management.
 *
 * Usage:
 *   deus solution list                    List recent solutions
 *   deus solution search <query>          Search by text/tags
 *   deus solution search --tag <tag> <q>  Search with tag filter
 *   deus solution add                     Add from JSON on stdin
 *   deus solution get <id>                Get a single solution by ID
 */

import {
  getSolution,
  listSolutions,
  searchSolutions,
  writeSolution,
} from './store.js';

import type { ProblemType, Severity, Solution } from './store.js';

function printSolution(sol: Solution): void {
  const tags = sol.tags.length > 0 ? ` [${sol.tags.join(', ')}]` : '';
  console.log(`${sol.id}  ${sol.title}${tags}`);
  console.log(`  type: ${sol.problemType}  severity: ${sol.severity}`);
  if (sol.module) console.log(`  module: ${sol.module}`);
}

function printSolutionFull(sol: Solution): void {
  printSolution(sol);
  console.log('');
  if (sol.problemType === 'knowledge') {
    console.log(`  Context: ${sol.symptoms}`);
    console.log(`  Guidance: ${sol.solution}`);
    console.log(`  When to Apply: ${sol.prevention}`);
  } else {
    console.log(`  Symptoms: ${sol.symptoms}`);
    if (sol.deadEnds) console.log(`  Dead Ends: ${sol.deadEnds}`);
    console.log(`  Solution: ${sol.solution}`);
    console.log(`  Prevention: ${sol.prevention}`);
  }
}

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (chunk) => (data += chunk));
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
    // If stdin is a TTY (no piped input), prompt the user
    if (process.stdin.isTTY) {
      console.error(
        'Paste JSON on stdin and press Ctrl+D, or pipe from a file:\n' +
          '  echo \'{"title":"...","tags":[...],...}\' | deus solution add',
      );
    }
  });
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const command = args[0] || 'list';

  switch (command) {
    case 'list': {
      const limit = parseInt(args[1] || '20', 10);
      const solutions = listSolutions(limit);
      if (solutions.length === 0) {
        console.log('No solutions found.');
        return;
      }
      for (const sol of solutions) {
        printSolution(sol);
        console.log('');
      }
      break;
    }

    case 'search': {
      const tagIdx = args.indexOf('--tag');
      let tags: string[] | undefined;
      let query: string;
      if (tagIdx > 0 && args[tagIdx + 1]) {
        tags = [args[tagIdx + 1]];
        // Query is everything except --tag and its value
        const remaining = [...args.slice(1, tagIdx), ...args.slice(tagIdx + 2)];
        query = remaining.join(' ');
      } else {
        query = args.slice(1).join(' ');
      }
      const results = searchSolutions(query, tags);
      if (results.length === 0) {
        console.log('No matching solutions.');
        return;
      }
      for (const sol of results) {
        printSolution(sol);
        console.log('');
      }
      break;
    }

    case 'get': {
      const id = args[1];
      if (!id) {
        console.error('Usage: deus solution get <id>');
        process.exit(1);
      }
      const sol = getSolution(id);
      if (!sol) {
        console.error(`Solution not found: ${id}`);
        process.exit(1);
      }
      printSolutionFull(sol);
      break;
    }

    case 'add': {
      try {
        const raw = await readStdin();
        const data = JSON.parse(raw) as {
          title: string;
          tags?: string[];
          problemType?: ProblemType;
          problem_type?: ProblemType;
          module?: string;
          severity?: Severity;
          symptoms: string;
          deadEnds?: string;
          dead_ends?: string;
          solution: string;
          prevention: string;
        };

        if (!data.title || !data.symptoms || !data.solution) {
          console.error(
            'Required fields: title, symptoms, solution, prevention',
          );
          process.exit(1);
        }

        const id = writeSolution({
          title: data.title,
          tags: data.tags || [],
          problemType: data.problemType || data.problem_type || 'bug',
          module: data.module,
          severity: data.severity || 'medium',
          symptoms: data.symptoms,
          deadEnds: data.deadEnds || data.dead_ends || '',
          solution: data.solution,
          prevention: data.prevention || '',
        });

        console.log(`Solution written: ${id}`);
      } catch (err) {
        console.error(
          `Failed to add solution: ${err instanceof Error ? err.message : String(err)}`,
        );
        process.exit(1);
      }
      break;
    }

    default:
      console.log('Usage: deus solution <list|search|add|get>');
      console.log('');
      console.log('  deus solution list [limit]       List recent solutions');
      console.log(
        '  deus solution search <query>     Search solutions by text',
      );
      console.log('  deus solution search --tag <t>   Search with tag filter');
      console.log('  deus solution add                Add from JSON stdin');
      console.log(
        '  deus solution get <id>           Show full solution details',
      );
      process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
