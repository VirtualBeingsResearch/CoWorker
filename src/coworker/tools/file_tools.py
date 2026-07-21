from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path

from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.tools.base import Tool, ToolDefinition

_GREP_OUTPUT_LIMIT = 3_000  # 单次返回字符上限
_READ_FILE_CHAR_LIMIT = 5_000  # read_file 单次返回字符上限


class ReadFileTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description="读取本地文件的内容，可通过 offset/limit 按行切片读取长文件",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "offset": {
                        "type": "integer",
                        "description": "起始行号（1-based），默认 1（从文件开头读取）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多读取的行数，0 或省略表示读取到文件末尾",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, path: str, offset: int = 1, limit: int = 0, **_) -> ToolResult:
        try:
            text = Path(path).read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            start = max(0, offset - 1)
            end = start + limit if limit > 0 else len(lines)
            chunk = "".join(lines[start:end])
            if len(chunk) > _READ_FILE_CHAR_LIMIT:
                truncated = chunk[:_READ_FILE_CHAR_LIMIT]
                shown_lines = end - start - chunk[_READ_FILE_CHAR_LIMIT:].count("\n")
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"{truncated}\n\n"
                        + tr(
                            "tool_result.file.read_truncated",
                            total=len(lines),
                            shown=shown_lines,
                        )
                    ),
                )
            return ToolResult(tool_call_id="", content=chunk)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class WriteFileTool(Tool):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            description=(
                "写入或修改本地文件。"
                "· 全量写入/追加：提供 content，append=true 时追加。"
                "· 局部替换（patch）：提供 old_string + new_string，精确替换文件中的一段文本，"
                "比全量写入节省 token。old_string 必须在文件中唯一出现，除非 replace_all=true。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "全量写入时的内容"},
                    "append": {
                        "type": "boolean",
                        "description": "是否追加而非覆盖，默认 false（仅全量写入时有效）",
                    },
                    "old_string": {"type": "string", "description": "局部替换：要被替换的原始文本"},
                    "new_string": {"type": "string", "description": "局部替换：替换后的新文本"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "局部替换：是否替换所有匹配项，默认 false",
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(
        self,
        path: str,
        content: str | None = None,
        append: bool = False,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        **_,
    ) -> ToolResult:
        async with self._lock:
            return await self._execute_locked(
                path, content, append, old_string, new_string, replace_all
            )

    async def _execute_locked(
        self,
        path: str,
        content: str | None,
        append: bool,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool,
    ) -> ToolResult:
        try:
            p = Path(path)
            if old_string is not None:
                # patch 模式
                if new_string is None:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("tool_result.file.patch_needs_new"),
                        is_error=True,
                    )
                text = p.read_text(encoding="utf-8")
                count = text.count(old_string)
                if count == 0:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("tool_result.file.patch_not_found"),
                        is_error=True,
                    )
                if count > 1 and not replace_all:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("tool_result.file.patch_ambiguous", count=count),
                        is_error=True,
                    )
                replaced = count if replace_all else 1
                new_text = (
                    text.replace(old_string, new_string)
                    if replace_all
                    else text.replace(old_string, new_string, 1)
                )
                p.write_text(new_text, encoding="utf-8")
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.file.replaced", count=replaced, path=path),
                )
            else:
                # 全量写入模式
                if content is None:
                    return ToolResult(
                        tool_call_id="",
                        content=tr("tool_result.file.needs_content"),
                        is_error=True,
                    )
                p.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if append else "w"
                p.open(mode, encoding="utf-8").write(content)
                return ToolResult(
                    tool_call_id="", content=tr("tool_result.file.written", path=path)
                )
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class ListDirectoryTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_directory",
            description="列出目录中的文件和子目录，显示类型、大小和修改时间",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认为当前目录"},
                    "show_hidden": {
                        "type": "boolean",
                        "description": "是否显示隐藏文件（以 . 开头），默认 false",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, path: str = ".", show_hidden: bool = False, **_) -> ToolResult:
        try:
            p = Path(path).resolve()
            if not p.exists():
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.file.path_missing", path=path),
                    is_error=True,
                )
            if not p.is_dir():
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.file.not_directory", path=path),
                    is_error=True,
                )

            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
            lines = [tr("tool_result.file.directory", path=p), ""]
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    stat = entry.stat()
                    size = stat.st_size
                    if entry.is_dir():
                        lines.append(tr("tool_result.file.directory_entry", name=entry.name))
                    else:
                        if size < 1024:
                            size_str = f"{size}B"
                        elif size < 1024 * 1024:
                            size_str = f"{size / 1024:.1f}KB"
                        else:
                            size_str = f"{size / 1024 / 1024:.1f}MB"
                        lines.append(
                            tr("tool_result.file.file_entry", name=entry.name, size=size_str)
                        )
                except OSError:
                    lines.append(f"[?]     {entry.name}")

            return ToolResult(tool_call_id="", content="\n".join(lines))
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


_FIND_MAX_SCAN = 50_000


class FindFilesTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="find_files",
            description="在目录树中按文件名 glob 模式查找文件（如 *.py、**/*.json）",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "文件名 glob 模式，例如 *.py 或 **/*.json",
                    },
                    "root": {"type": "string", "description": "搜索根目录，默认为当前目录"},
                    "max_results": {"type": "integer", "description": "最多返回的结果数，默认 50"},
                },
                "required": ["pattern"],
            },
        )

    async def execute(
        self, pattern: str, root: str = ".", max_results: int = 50, **_
    ) -> ToolResult:
        try:
            root_path = Path(root).resolve()
            if not root_path.exists():
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.file.root_missing", path=root),
                    is_error=True,
                )

            name_pattern = Path(pattern).name if ("/" in pattern or "\\" in pattern) else pattern

            def _walk() -> tuple[list[str], str]:
                results: list[str] = []
                scanned = 0
                for dirpath, _, filenames in os.walk(root_path):
                    for name in filenames:
                        scanned += 1
                        if scanned >= _FIND_MAX_SCAN:
                            return results, "scan"
                        if fnmatch.fnmatch(name, name_pattern):
                            results.append(str(Path(dirpath, name).relative_to(root_path)))
                            if len(results) >= max_results:
                                return results, "results"
                return results, ""

            results, stop_reason = await asyncio.to_thread(_walk)

            if not results:
                return ToolResult(
                    tool_call_id="", content=tr("tool_result.file.find_none", pattern=pattern)
                )

            lines = [
                tr(
                    "tool_result.file.find_header",
                    root=root_path,
                    pattern=pattern,
                    count=len(results),
                )
            ]
            lines += sorted(results)
            if stop_reason == "results":
                lines.append(tr("tool_result.file.result_limit", limit=max_results))
            elif stop_reason == "scan":
                lines.append(tr("tool_result.file.scan_limit", limit=_FIND_MAX_SCAN))
            return ToolResult(tool_call_id="", content="\n".join(lines))
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)


class GrepFilesTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep_files",
            description="在文件或目录中搜索匹配正则表达式的行，返回文件名、行号和匹配内容",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "要搜索的正则表达式（或普通字符串）",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目标：文件路径或目录路径，默认当前目录",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "只搜索匹配此 glob 的文件，如 *.py，默认搜索所有文件",
                    },
                    "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
                    "context_lines": {
                        "type": "integer",
                        "description": "每个匹配行前后额外显示的行数，默认 0",
                    },
                    "max_matches": {"type": "integer", "description": "最多返回的匹配数，默认 50"},
                },
                "required": ["pattern"],
            },
        )

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str = "*",
        ignore_case: bool = False,
        context_lines: int = 0,
        max_matches: int = 50,
        **_,
    ) -> ToolResult:
        try:
            flags = re.IGNORECASE if ignore_case else 0
            regex = re.compile(pattern, flags)
            target = Path(path).resolve()

            if not target.exists():
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.file.path_missing", path=path),
                    is_error=True,
                )

            files: list[Path]
            if target.is_file():
                files = [target]
            else:
                files = sorted(target.rglob(file_pattern))

            hits: list[str] = []
            total = 0
            output_chars = 0
            output_truncated = False

            for file in files:
                if not file.is_file():
                    continue
                try:
                    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue

                rel = str(file.relative_to(target)) if target.is_dir() else file.name
                i = 0
                while i < len(lines):
                    if regex.search(lines[i]):
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        chunk: list[str] = []
                        for j in range(start, end):
                            prefix = ">" if j == i else " "
                            chunk.append(f"{rel}:{j + 1}{prefix} {lines[j]}")
                        if context_lines:
                            chunk.append("--")
                        chunk_chars = sum(len(line) + 1 for line in chunk)
                        if output_chars + chunk_chars > _GREP_OUTPUT_LIMIT:
                            output_truncated = True
                            if output_chars == 0:
                                # 第一条匹配本身就超限，截断后仍返回，避免空结果
                                hits.append(chunk[0][:_GREP_OUTPUT_LIMIT])
                                total += 1
                            break
                        hits.extend(chunk)
                        output_chars += chunk_chars
                        total += 1
                        if total >= max_matches:
                            break
                        i = end  # skip past context to avoid duplicates
                    else:
                        i += 1

                if total >= max_matches or output_truncated:
                    break

            if not hits:
                return ToolResult(
                    tool_call_id="", content=tr("tool_result.file.grep_none", pattern=pattern)
                )

            header = tr("tool_result.file.grep_header", pattern=pattern, count=total)
            if output_truncated:
                header += tr("tool_result.file.grep_output_limit", limit=_GREP_OUTPUT_LIMIT)
            elif total >= max_matches:
                header += tr("tool_result.file.grep_match_limit", limit=max_matches)
            return ToolResult(tool_call_id="", content="\n".join([header, ""] + hits))
        except re.error as e:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.file.regex_error", error=e),
                is_error=True,
            )
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)
