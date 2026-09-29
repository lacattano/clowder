// Run: node tests/diff.test.mjs <installed-pi-package-directory>
// The package directory is where jiti and @earendil-works/pi-tui resolve, normally the
// global @earendil-works/pi-coding-agent install:
//   node tests/diff.test.mjs "C:/Users/<you>/AppData/Roaming/npm/node_modules/@earendil-works/pi-coding-agent"
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, resolve, join } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..');
const root = resolve(process.argv[2]);
const requirePi = createRequire(join(root, 'package.json'));
const { createJiti } = requirePi('jiti');
const tuiPath = requirePi.resolve('@earendil-works/pi-tui');
const jiti = createJiti(import.meta.url, { alias: { '@earendil-works/pi-tui': tuiPath } });
const { default: register, parseDiff, colourLine, DiffViewer, explainPrompt, parseRequest, describePlace, translateGitError, emptyMessage } =
  await jiti.import(resolve(repo, 'extensions/diff.ts'));
const { visibleWidth } = await import(pathToFileURL(tuiPath).href);
const theme = {
  fg: (colour, text) => `\x1b[${({toolDiffAdded: 32, toolDiffRemoved: 31, accent: 36})[colour] ?? 90}m${text}\x1b[39m`,
  bold: text => `\x1b[1m${text}\x1b[22m`,
};

// Arguments: the bare form and --cached keep their meaning; one revision or range is
// the committed diff; the two cannot be combined.
assert.deepEqual(parseRequest(''), { staged: false, stat: false, detail: false, rev: '' });
assert.deepEqual(parseRequest('--cached'), { staged: true, stat: false, detail: false, rev: '' });
assert.deepEqual(parseRequest('--staged'), { staged: true, stat: false, detail: false, rev: '' });
assert.deepEqual(parseRequest('main...HEAD'), { staged: false, stat: false, detail: false, rev: 'main...HEAD' });
assert.deepEqual(parseRequest('abc1234'), { staged: false, stat: false, detail: false, rev: 'abc1234' });
assert.deepEqual(parseRequest('--stat main...HEAD'), { staged: false, stat: true, detail: false, rev: 'main...HEAD' });
assert.equal(typeof parseRequest('--cached main...HEAD'), 'string');
assert.equal(typeof parseRequest('main HEAD'), 'string');
assert.equal(typeof parseRequest('--bogus'), 'string');
assert.equal(parseRequest('--detail').detail, true);

// Where the viewer is, and what base it uses. A working viewer can be told from a
// broken one by this line alone.
const fakeExec = answers => async (command, args) => {
  assert.equal(command, 'git');
  const joined = args.filter(item => item !== '--no-pager').join(' ');
  if (!(joined in answers)) return {stdout: '', stderr: '', code: 1, killed: false};
  return {stdout: answers[joined] + '\n', stderr: '', code: 0, killed: false};
};
const branchPlace = await describePlace(fakeExec({
  'rev-parse --is-inside-work-tree': 'true',
  'rev-parse --abbrev-ref HEAD': 'task/refund',
  'rev-parse --short HEAD': 'abc1234',
  'rev-parse HEAD': 'abc1234def',
  'rev-parse --absolute-git-dir': '/repo/.git',
  'rev-parse --path-format=absolute --git-common-dir': '/repo/.git/worktrees/wt',
  'rev-parse --verify --quiet main': 'abc1234def',
  'rev-parse --short main': 'abc1234',
}), '/repo/wt');
assert.match(branchPlace.line, /branch task\/refund at abc1234/);
assert.match(branchPlace.line, /base main@abc1234/);
assert(!branchPlace.shared, 'a linked worktree is not the shared checkout');

const pinnedPlace = await describePlace(fakeExec({
  'rev-parse --is-inside-work-tree': 'true',
  'rev-parse --abbrev-ref HEAD': 'HEAD',
  'rev-parse --short HEAD': 'deadbee',
  'rev-parse HEAD': 'deadbeefull',
  'rev-parse --absolute-git-dir': '/repo/.git',
  'rev-parse --path-format=absolute --git-common-dir': '/repo/.git/worktrees/wt',
  'rev-parse --verify --quiet main': 'abc1234def',
  'rev-parse --short main': 'abc1234',
}), '/repo/wt', JSON.stringify({jobs: {'j-0001': {review_commit: 'deadbeefull', branch: 'task/refund'}}}));
assert.match(pinnedPlace.line, /no branch, pinned at deadbee/);
assert.match(pinnedPlace.line, /the save from task\/refund/);
assert.match(pinnedPlace.line, /base main@abc1234/);

const freePlace = await describePlace(fakeExec({
  'rev-parse --is-inside-work-tree': 'true',
  'rev-parse --abbrev-ref HEAD': 'HEAD',
  'rev-parse --short HEAD': 'abc1234',
  'rev-parse HEAD': 'abc1234def',
  'rev-parse --absolute-git-dir': '/repo/.git',
  'rev-parse --path-format=absolute --git-common-dir': '/repo/.git/worktrees/wt',
  'rev-parse --verify --quiet main': 'abc1234def',
  'rev-parse --short main': 'abc1234',
}), '/repo/wt');
assert.match(freePlace.line, /free space, no branch, sitting at main@abc1234/);
assert.equal(emptyMessage({staged: false, stat: false, detail: false, rev: ''}, freePlace), 'no changes: this copy is not on a job branch');
assert.match(translateGitError("fatal: bad revision 'nope'", {staged: false, stat: false, detail: false, rev: 'nope'}), /does not know nope/);
assert.match(translateGitError('fatal: not a git repository (or any parent)', {staged: false, stat: false, detail: false, rev: ''}), /not a git repository/);
assert.match(translateGitError('fatal: your current branch does not have any commits yet', {staged: false, stat: false, detail: false, rev: ''}), /no commits yet/);

const patch = 'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n' +
  Array.from({length: 80}, (_, i) => ` context ${i} \u65e5\u672c\u8a9e`).join('\n') + '\n' +
  'diff --git a/deleted.py b/deleted.py\ndeleted file mode 100644\n--- a/deleted.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n' +
  'diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n';
const files = parseDiff(patch);
assert.equal(files.length, 3);
assert.equal(files[0].title, 'b/a.py');
assert.equal(files[1].title, 'a/deleted.py');
assert.equal(parseDiff('').length, 0);
assert(colourLine(theme, '+new').includes('\x1b[32m'));
assert(colourLine(theme, '-old').includes('\x1b[31m'));
assert(colourLine(theme, '+++ b/a').includes('\x1b[36m'));
assert(!colourLine(theme, '+x\x1b[2J').includes('\x1b[2J'));
let rows = 24;
let choice;
const viewer = new DiffViewer(files, theme, () => rows, value => {choice = value;}, () => {});
const render = (width = 70) => {
  const lines = viewer.render(width);
  assert(lines.length <= Math.floor(rows * 0.85));
  assert(lines.every(line => visibleWidth(line) <= width));
  return lines.join('\n');
};
assert.match(render(), /File 1\/3/);
viewer.handleInput('\x1b[6~'); // PageDown
assert(!render().includes('Rows 1-'));
viewer.handleInput('n');
assert.match(render(), /File 2\/3/);
viewer.handleInput('b');
assert.match(render(), /Rows 1-/);
viewer.handleInput('\x1b[F'); // End
render();
viewer.handleInput('e');
assert.equal(choice.action, 'explain');
assert(choice.scroll > 0);
const resumed = new DiffViewer(files, theme, () => rows, value => {choice = value;}, () => {}, choice);
assert(!resumed.render(70).join('\n').includes('Rows 1-'));
for (const width of [1, 20, 32, 40, 100]) render(width);
rows = 8;
render(40);
viewer.handleInput('\x1b');
assert.equal(choice.action, 'finish');
assert.match(explainPrompt(files[0], 'Staged'), /Staged/);
assert.match(explainPrompt(files[0], 'Staged'), /\+new/);
assert.match(explainPrompt({ ...files[0], lines: ['x'.repeat(30000)] }, 'scope'), /truncated/);

// Mock host exercises command lifecycle without a model call or live Pi session.
let handler;
let diffCalls = 0;
let output = patch;
let code = 0;
let killed = false;
let busy = false;
let uiAction = 'e';
let lastArgs;
const notices = [];
const sent = [];
process.env.CLOWDER_STATE = join(tmpdir(), 'no-such-clowder-state.json');
const placeFor = {
  'rev-parse --is-inside-work-tree': 'true',
  'rev-parse --abbrev-ref HEAD': 'task/refund',
  'rev-parse --short HEAD': 'abc1234',
  'rev-parse HEAD': 'abc1234def',
  'rev-parse --absolute-git-dir': '/repo/.git',
  'rev-parse --path-format=absolute --git-common-dir': '/repo/.git/worktrees/wt',
  'rev-parse --verify --quiet main': 'abc1234def',
  'rev-parse --short main': 'abc1234',
  'rev-parse --verify --quiet origin/main': 'abc1234def',
  'rev-list --count main..origin/main': '0',
};
register({
  registerCommand(name, options) { assert.equal(name, 'diff'); handler = options.handler; },
  async exec(command, args, options) {
    assert.equal(command, 'git');
    assert.equal(options.cwd, 'repo');
    const joined = args.filter(item => item !== '--no-pager').join(' ');
    if (joined.startsWith('diff ')) {
      diffCalls++;
      lastArgs = args;
      assert(args.includes('--no-ext-diff'));
      return { stdout: output, stderr: '', code, killed };
    }
    const answer = placeFor[joined];
    return { stdout: answer ? answer + '\n' : '', stderr: '', code: answer ? 0 : 1, killed: false };
  },
  sendUserMessage(...args) { sent.push(args); },
});
const ctx = {
  cwd: 'repo', mode: 'tui', isIdle: () => !busy,
  ui: {
    notify: (...args) => notices.push(args),
    custom: async factory => {
      let value;
      const component = factory({terminal: {rows: 30}, requestRender() {}}, theme, {}, result => {value = result;});
      component.render(80);
      component.handleInput(uiAction);
      return value;
    },
  },
};
await handler('--invalid', ctx);
assert.equal(diffCalls, 0);
await handler('', {...ctx, mode: 'rpc'});
assert.equal(diffCalls, 0);
await handler('--cached', ctx);
assert.match(sent.at(-1)[0], /Staged changes/);
assert.equal(sent.length, 1);
assert(lastArgs.includes('--cached'));
busy = true;
await handler('', ctx);
assert.deepEqual(sent.at(-1)[1], {deliverAs: 'followUp'});
uiAction = 'q';
await handler('', ctx);
assert.equal(sent.length, 2);
output = '';
await handler('', ctx);
assert.match(notices.at(-1)[0], /Untracked files/);
code = 128;
await handler('', ctx);
assert.equal(notices.at(-1)[1], 'error');
assert.match(notices.at(-1)[0], /Git could not read this diff/);
assert(!notices.at(-1)[0].includes('\n    at '), 'a git failure must not print a traceback');
code = 0; killed = true;
await handler('', ctx);
assert.equal(notices.at(-1)[1], 'error');
killed = false; output = ' file.py | 2 +-\n';
await handler('--stat', ctx);
assert.equal(notices.at(-1)[0], output);

// A committed, unpushed branch: the same viewer, fetched from a revision or range.
output = patch; code = 0; killed = false; uiAction = 'e';
const beforeRev = diffCalls;
await handler('main...HEAD', ctx);
assert.equal(diffCalls, beforeRev + 1);
assert(lastArgs.includes('main...HEAD'));
assert(!lastArgs.includes('--cached'));
assert.match(sent.at(-1)[0], /Committed changes \(main\.\.\.HEAD\)/);
assert.match(sent.at(-1)[0], /\+new/);
assert.match(sent.at(-1)[0], /branch task\/refund/);
assert.match(sent.at(-1)[0], /base main@abc1234/);

uiAction = 'q';
await handler('abc1234', ctx);
assert(lastArgs.includes('abc1234'));
assert(!lastArgs.includes('--cached'));

output = ' file.py | 2 +-\n';
await handler('--stat main...HEAD', ctx);
assert(lastArgs.includes('--stat'));
assert(lastArgs.includes('main...HEAD'));
assert.equal(notices.at(-1)[0], output);

// An empty range gives one line, not a bare git error.
output = '';
await handler('main...HEAD', ctx);
assert.equal(notices.at(-1)[0], 'No tracked changes for main...HEAD.');
output = patch;

const beforeBad = diffCalls;
await handler('--cached main...HEAD', ctx);
assert.equal(diffCalls, beforeBad);
assert.match(notices.at(-1)[0], /cannot be combined/);
await handler('main HEAD', ctx);
assert.equal(diffCalls, beforeBad);
assert.match(notices.at(-1)[0], /Usage/);

// Verify parsing against actual Git output in a disposable repository.
const temp = mkdtempSync(join(tmpdir(), 'pi-diff-review-'));
try {
  const git = (...args) => execFileSync('git', args, {cwd: temp, encoding: 'utf8'});
  git('init', '-q');
  writeFileSync(join(temp, 'space name.py'), 'old\n');
  writeFileSync(join(temp, 'remove.py'), 'gone\n');
  git('add', '.');
  git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'baseline');
  writeFileSync(join(temp, 'space name.py'), 'new\n');
  rmSync(join(temp, 'remove.py'));
  const actual = parseDiff(git('--no-pager', 'diff', '--no-ext-diff', '--no-textconv', '--no-color'));
  assert.equal(actual.length, 2);
  assert(actual.some(file => file.title === 'b/space name.py'));
  git('add', '.');
  assert.equal(parseDiff(git('diff', '--cached')).length, 2);
  assert.equal(parseDiff(git('diff')).length, 0);
} finally { rmSync(temp, {recursive: true, force: true}); }

// The acceptance: a committed, unpushed branch reviewed against its base.
const branchTemp = mkdtempSync(join(tmpdir(), 'pi-diff-range-'));
try {
  const git = (...args) => execFileSync('git', args, {cwd: branchTemp, encoding: 'utf8'});
  const commit = (...args) => git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null', ...args);
  git('init', '-q', '-b', 'main');
  writeFileSync(join(branchTemp, 'feature.py'), 'before\n');
  git('add', '.');
  commit('commit', '-qm', 'baseline');
  git('checkout', '-q', '-b', 'task/unpushed');
  writeFileSync(join(branchTemp, 'feature.py'), 'after\n');
  writeFileSync(join(branchTemp, 'added.py'), 'new file\n');
  git('add', '.');
  commit('commit', '-qm', 'branch work');
  const range = parseDiff(git('--no-pager', 'diff', '--no-ext-diff', '--no-textconv', '--no-color', 'main...HEAD'));
  assert.equal(range.length, 2);
  assert(range.some(file => file.title === 'b/feature.py'));
  assert(range.some(file => file.title === 'b/added.py'));
} finally { rmSync(branchTemp, {recursive: true, force: true}); }

// The place line, against real Git: a job worktree, then its pinned copy.
const placeTemp = mkdtempSync(join(tmpdir(), 'pi-diff-place-'));
try {
  const run = (cwd, ...args) => execFileSync('git', args, {cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']});
  const commit = (cwd, ...args) => run(cwd, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null', ...args);
  run(placeTemp, 'init', '-q', '-b', 'main');
  writeFileSync(join(placeTemp, 'base.py'), 'one\n');
  run(placeTemp, 'add', '.');
  commit(placeTemp, 'commit', '-qm', 'baseline');
  const space = join(placeTemp, '.worktrees', 'agent');
  run(placeTemp, 'worktree', 'add', '-q', '-b', 'task/job', space);
  writeFileSync(join(space, 'feature.py'), 'two\n');
  run(space, 'add', '.');
  commit(space, 'commit', '-qm', 'job work');
  const realExec = (command, args, options) => {
    try {
      const stdout = execFileSync(command, args, {cwd: options.cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']});
      return {stdout, stderr: '', code: 0, killed: false};
    } catch (error) {
      return {stdout: error.stdout ?? '', stderr: error.stderr ?? '', code: typeof error.status === 'number' ? error.status : 1, killed: false};
    }
  };
  const jobPlace = await describePlace(realExec, space);
  assert.match(jobPlace.line, /branch task\/job at /);
  assert.match(jobPlace.line, /base main@/);
  run(space, 'checkout', '-q', '--detach');
  const pinned = await describePlace(realExec, space);
  assert.match(pinned.line, /no branch, pinned at /);
  assert.match(pinned.line, /base main@/);
} finally { rmSync(placeTemp, {recursive: true, force: true}); }

console.log('PASS: parser, colours, control characters, scrolling, navigation, resume, width/height, Explain scope and limits, place and base naming, translated git errors, command errors, argument parsing for revisions and ranges, actual Git staged/unstaged/deleted/spaced paths, and a committed unpushed branch range.');
