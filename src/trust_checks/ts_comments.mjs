import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require(process.argv[2]);

const TRIVIA = new Set([
  ts.SyntaxKind.WhitespaceTrivia,
  ts.SyntaxKind.NewLineTrivia,
  ts.SyntaxKind.ShebangTrivia,
  ts.SyntaxKind.ConflictMarkerTrivia,
]);
const COMMENTS = new Map([
  [ts.SyntaxKind.SingleLineCommentTrivia, "line"],
  [ts.SyntaxKind.MultiLineCommentTrivia, "block"],
]);
const JSX_TEXT = new Set([ts.SyntaxKind.JsxText, ts.SyntaxKind.JsxTextAllWhiteSpaces]);

function scriptKind(name) {
  if (name.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (name.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (name.endsWith(".ts") || name.endsWith(".mts") || name.endsWith(".cts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}

function isJSDocNode(node) {
  return node.kind >= ts.SyntaxKind.FirstJSDocNode && node.kind <= ts.SyntaxKind.LastJSDocNode;
}

function leafTokens(sourceFile) {
  const leaves = [];
  const stack = [sourceFile];
  while (stack.length > 0) {
    const node = stack.pop();
    const children = node.getChildren(sourceFile).filter((child) => !isJSDocNode(child));
    if (children.length === 0) {
      leaves.push(node);
      continue;
    }
    for (let index = children.length - 1; index >= 0; index -= 1) stack.push(children[index]);
  }
  return leaves;
}

function scanFile(name, text) {
  const kind = scriptKind(name);
  const sourceFile = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, kind);
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, sourceFile.languageVariant, text);
  const found = [];
  const leaves = leafTokens(sourceFile);
  const firstCode = leaves.find(
    (token) => token.kind !== ts.SyntaxKind.EndOfFileToken && token.end > token.getStart(sourceFile),
  );
  const codeStart = firstCode === undefined ? text.length : firstCode.getStart(sourceFile);
  for (const token of leaves) {
    if (JSX_TEXT.has(token.kind)) continue;
    const limit = token.kind === ts.SyntaxKind.EndOfFileToken ? text.length : token.getStart(sourceFile);
    scanner.resetTokenState(token.pos);
    while (scanner.getTokenEnd() < limit) {
      const tokenKind = scanner.scan();
      const style = COMMENTS.get(tokenKind);
      if (style !== undefined) {
        const start = scanner.getTokenStart();
        const end = scanner.getTokenEnd();
        found.push({
          start,
          style,
          text: text.slice(start, end),
          leading: end <= codeStart,
          line: sourceFile.getLineAndCharacterOfPosition(start).line + 1,
          endLine: sourceFile.getLineAndCharacterOfPosition(end).line + 1,
        });
        continue;
      }
      if (!TRIVIA.has(tokenKind)) break;
    }
  }
  found.sort((left, right) => left.start - right.start);
  return found.map(({ style, text: body, line, endLine, leading }) => ({ style, text: body, line, endLine, leading }));
}

const input = JSON.parse(await Bun.stdin.text());
const output = input.map(({ name, text }) => {
  try {
    return { comments: scanFile(name, text), error: null };
  } catch (error) {
    return { comments: [], error: String(error) };
  }
});
process.stdout.write(JSON.stringify(output));
