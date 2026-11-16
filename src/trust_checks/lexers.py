from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass


@dataclass(frozen=True)
class RawComment:
    line: int
    end_line: int
    body: str
    style: str


class LexError(Exception):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def python_comments(text: str) -> list[RawComment]:
    found: list[RawComment] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            row = token.start[0]
            if row == 1 and token.string.startswith("#!"):
                continue
            if row <= 2 and _CODING_RE.match(token.line):
                continue
            found.append(RawComment(row, row, token.string[1:], "hash"))
        tree = ast.parse(text)
    except SyntaxError as error:
        raise LexError(error.lineno or 1, f"Python 문법 오류: {error.msg}") from error
    except tokenize.TokenError as error:
        line = error.args[1][0] if len(error.args) > 1 else 1
        raise LexError(line, f"Python 토큰 오류: {error.args[0]}") from error
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            found.append(RawComment(first.lineno, first.end_lineno or first.lineno, first.value.value, "doc"))
    found.sort(key=lambda comment: comment.line)
    return found


_SHELL_BREAK = frozenset(" \t\n;&|()<>")


def _skip_quoted(text: str, start: int, closer: str, escapes: bool) -> int:
    index = start
    while index < len(text):
        char = text[index]
        if escapes and char == "\\":
            index += 2
            continue
        if char == closer:
            return index + 1
        index += 1
    return len(text)


def _skip_heredocs(text: str, start: int, pending: list[tuple[str, bool]]) -> int:
    index = start
    for delimiter, strip_tabs in pending:
        while index < len(text):
            end = text.find("\n", index)
            stop = len(text) if end == -1 else end
            line = text[index:stop]
            index = stop + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    pending.clear()
    return min(index, len(text))


def _heredoc_word(text: str, start: int) -> tuple[str, int] | None:
    index = start
    parts: list[str] = []
    while index < len(text) and text[index] not in _SHELL_BREAK:
        char = text[index]
        if char == "'":
            end = text.find("'", index + 1)
            end = len(text) if end == -1 else end
            parts.append(text[index + 1 : end])
            index = end + 1
        elif char == '"':
            end = _skip_quoted(text, index + 1, '"', escapes=True)
            parts.append(text[index + 1 : end - 1].replace("\\", ""))
            index = end
        elif char == "\\":
            parts.append(text[index + 1 : index + 2])
            index += 2
        else:
            parts.append(char)
            index += 1
    if index == start:
        return None
    return "".join(parts), index


class _Shell:
    def __init__(self, text: str) -> None:
        self.text = text
        self.found: list[RawComment] = []
        self.pending: list[tuple[str, bool]] = []

    def word_start(self, index: int) -> bool:
        return index == 0 or self.text[index - 1] in _SHELL_BREAK

    def arithmetic(self, index: int) -> int:
        depth = 0
        text = self.text
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return len(text)

    def comment(self, index: int) -> int:
        text = self.text
        end = text.find("\n", index)
        stop = len(text) if end == -1 else end
        if not (index == 0 and text.startswith("#!")):
            line = text.count("\n", 0, index) + 1
            self.found.append(RawComment(line, line, text[index + 1 : stop], "hash"))
        return stop

    def double(self, index: int) -> int:
        text = self.text
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
            elif char == '"':
                return index + 1
            elif text.startswith("$((", index):
                index = self.arithmetic(index + 1)
            elif text.startswith("$(", index):
                index = self.commands(index + 2, ")")
            elif char == "`":
                index = self.commands(index + 1, "`")
            else:
                index += 1
        return len(text)

    def heredoc(self, index: int) -> int:
        text = self.text
        cursor = index + 2
        strip_tabs = text.startswith("-", cursor)
        cursor += strip_tabs
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
        word = _heredoc_word(text, cursor)
        if word is None:
            return index + 2
        self.pending.append((word[0], strip_tabs))
        return word[1]

    def commands(self, index: int, closer: str | None) -> int:
        text = self.text
        depth = 0
        while index < len(text):
            char = text[index]
            if char == "\n":
                index += 1
                if self.pending:
                    index = _skip_heredocs(text, index, self.pending)
            elif char == "\\":
                index += 2
            elif char == "'":
                index = _skip_quoted(text, index + 1, "'", escapes=False)
            elif text.startswith("$'", index):
                index = _skip_quoted(text, index + 2, "'", escapes=True)
            elif char == '"':
                index = self.double(index + 1)
            elif char == "`":
                if closer == "`":
                    return index + 1
                index = self.commands(index + 1, "`")
            elif text.startswith("$((", index):
                index = self.arithmetic(index + 1)
            elif text.startswith("((", index) and self.word_start(index):
                index = self.arithmetic(index)
            elif text.startswith("$(", index):
                index = self.commands(index + 2, ")")
            elif char == "(":
                depth += 1
                index += 1
            elif char == ")":
                if depth == 0 and closer == ")":
                    return index + 1
                depth = max(0, depth - 1)
                index += 1
            elif char == "#" and self.word_start(index):
                index = self.comment(index)
            elif text.startswith("<<<", index):
                index += 3
            elif text.startswith("<<", index):
                index = self.heredoc(index)
            else:
                index += 1
        return len(text)


def shell_comments(text: str) -> list[RawComment]:
    scanner = _Shell(text)
    scanner.commands(0, None)
    return scanner.found


_BLOCK_SCALAR_RE = re.compile(r"(?:^|:[ \t]+|-[ \t]+|\?[ \t]+)(?:[&!]\S*[ \t]+)*[|>](?P<mods>[-+0-9]*)[ \t]*$")
_NODE_PREFIX_RE = re.compile(r"^( *)((?:-[ \t]+)*)")


@dataclass
class _YamlState:
    quote: str | None = None
    flow: int = 0


def _yaml_line(line: str, state: _YamlState) -> int | None:
    index = 0
    plain = False
    closed_at = -1
    while index < len(line):
        char = line[index]
        if state.quote == "'":
            if char == "'":
                if line.startswith("''", index):
                    index += 2
                    continue
                state.quote = None
                closed_at = index + 1
            index += 1
            continue
        if state.quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                state.quote = None
                closed_at = index + 1
            index += 1
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return index
        if char in " \t":
            index += 1
            continue
        following = line[index + 1 : index + 2]
        if plain:
            if char == ":" and following in ("", " ", "\t"):
                plain = False
            elif state.flow and char in ",]}":
                plain = False
                state.flow = max(0, state.flow - (char in "]}"))
            index += 1
            continue
        if char in "'\"":
            state.quote = char
        elif char in "[{":
            state.flow += 1
        elif char in "]}":
            state.flow = max(0, state.flow - 1)
        elif char == ":" and (following in ("", " ", "\t") or state.flow or closed_at == index):
            pass
        elif char in "-?" and following in ("", " ", "\t"):
            pass
        elif char == ",":
            pass
        elif char in "&!*":
            while index + 1 < len(line) and line[index + 1] not in " \t":
                index += 1
        else:
            plain = True
        index += 1
    return None


def _node_indent(content: str) -> int:
    match = _NODE_PREFIX_RE.match(content)
    if match is None:
        return 0
    rest = content[match.end() :].lstrip()
    if rest[:1] not in ("|", ">", "&", "!"):
        return match.end()
    if match.group(2):
        return match.group(0).rstrip().rfind("-")
    return len(match.group(1))


def yaml_comments(text: str) -> list[RawComment]:
    found: list[RawComment] = []
    state = _YamlState()
    block_parent: int | None = None
    block_content: int | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        indent = len(line) - len(line.lstrip(" "))
        if block_parent is not None:
            if not line.strip():
                continue
            if block_content is None and indent > block_parent:
                block_content = indent
                continue
            if block_content is not None and indent >= block_content:
                continue
            block_parent = None
            block_content = None
        position = _yaml_line(line, state)
        content = line if position is None else line[:position]
        if position is not None:
            found.append(RawComment(number, number, line[position + 1 :], "hash"))
        header = _BLOCK_SCALAR_RE.search(content.strip()) if state.quote is None else None
        if header is not None:
            block_parent = _node_indent(content)
            digits = re.sub(r"[^0-9]", "", header.group("mods"))
            block_content = block_parent + int(digits) if digits else None
    return found


def _toml_multiline(text: str, start: int, closer: str, escapes: bool) -> int:
    index = start
    while index < len(text):
        if escapes and text[index] == "\\":
            index += 2
            continue
        if text.startswith(closer, index):
            index += 3
            while index < len(text) and text[index] == closer[0]:
                index += 1
            return index
        index += 1
    return len(text)


def _toml_single(text: str, start: int, closer: str, escapes: bool) -> int:
    index = start
    while index < len(text) and text[index] != "\n":
        if escapes and text[index] == "\\":
            index += 2
            continue
        if text[index] == closer:
            return index + 1
        index += 1
    return index


def toml_comments(text: str) -> list[RawComment]:
    found: list[RawComment] = []
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith('"""', index):
            index = _toml_multiline(text, index + 3, '"""', escapes=True)
        elif text.startswith("'''", index):
            index = _toml_multiline(text, index + 3, "'''", escapes=False)
        elif char == '"':
            index = _toml_single(text, index + 1, '"', escapes=True)
        elif char == "'":
            index = _toml_single(text, index + 1, "'", escapes=False)
        elif char == "#":
            end = text.find("\n", index)
            stop = len(text) if end == -1 else end
            line = text.count("\n", 0, index) + 1
            found.append(RawComment(line, line, text[index + 1 : stop], "hash"))
            index = stop
        else:
            index += 1
    return found


_RUST_RAW_RE = re.compile(r'(?:br|cr|r)(#*)"')
_RUST_PREFIXED_RE = re.compile(r'[bc]"')


def _is_ident(char: str) -> bool:
    return char.isalnum() or char == "_"


def _rust_block_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text) and depth:
        if text.startswith("/*", index):
            depth += 1
            index += 2
        elif text.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def line_body(text: str) -> str:
    body = text[2:]
    return body[1:] if body[:1] in ("/", "!") else body


def block_body(text: str) -> str:
    inner = text[2:-2] if text.endswith("*/") else text[2:]
    if inner.startswith(("*", "!")):
        inner = inner[1:]
    lines = [part.strip().removeprefix("*").strip() for part in inner.split("\n")]
    return "\n".join(lines).strip()


def rust_comments(text: str) -> list[RawComment]:
    found: list[RawComment] = []
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith("//", index):
            end = text.find("\n", index)
            stop = len(text) if end == -1 else end
            line = text.count("\n", 0, index) + 1
            found.append(RawComment(line, line, line_body(text[index:stop]), "line"))
            index = stop
            continue
        if text.startswith("/*", index):
            stop = _rust_block_end(text, index + 2)
            line = text.count("\n", 0, index) + 1
            end_line = text.count("\n", 0, stop) + 1
            found.append(RawComment(line, end_line, block_body(text[index:stop]), "block"))
            index = stop
            continue
        boundary = index == 0 or not _is_ident(text[index - 1])
        if boundary and char in "brc":
            raw = _RUST_RAW_RE.match(text, index)
            if raw:
                closer = '"' + raw.group(1)
                end = text.find(closer, raw.end())
                index = len(text) if end == -1 else end + len(closer)
                continue
            if _RUST_PREFIXED_RE.match(text, index):
                index = _skip_quoted(text, index + 2, '"', escapes=True)
                continue
        if char == '"':
            index = _skip_quoted(text, index + 1, '"', escapes=True)
            continue
        if char == "'":
            if text.startswith("\\", index + 1):
                end = text.find("'", index + 3)
                index = len(text) if end == -1 else end + 1
            elif text[index + 2 : index + 3] == "'":
                index += 3
            else:
                index += 1
            continue
        index += 1
    return found


_DOCKER_DIRECTIVE_RE = re.compile(r"#\s*(?:syntax|escape|check)\s*=", re.IGNORECASE)


def dockerfile_comments(text: str) -> list[RawComment]:
    found: list[RawComment] = []
    directives_open = True
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.lstrip()
        if directives_open and _DOCKER_DIRECTIVE_RE.match(stripped):
            continue
        directives_open = False
        if stripped.startswith("#"):
            found.append(RawComment(number, number, stripped[1:], "hash"))
    return found
