"""工具集：Schema 定义 + 实现 + 安全边界。

设计要点：
1. 路径必须 resolve 到工作目录内，防止 ../ 逃逸。
2. shell 命令走白名单 + 不经过 shell，防止 `;` `|` `&&` 串联注入。
3. 单个工具输出有上限，超出截断（头 + 尾），避免一次撑爆上下文。
4. replace_in_file 用精确字符串替换而不是解析 diff —— 简单可靠，
   代价是要求旧文本在文件中唯一匹配。
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

MAX_OUTPUT_CHARS = 4000

# 白名单：只允许只读 / 分析类命令，且参数里出现危险字符直接拒
SHELL_WHITELIST = {
    "ls", "cat", "head", "tail", "grep", "wc", "find", "file",
    "git", "python", "python3", "node", "pwd", "echo",
}
SHELL_FORBIDDEN_SUBSTR = ["rm ", "rmdir", "sudo", "curl", "wget", "chmod", "chown",
                          "mkfs", "dd ", ">", ">>", "|", "&&", ";", "`", "$("]


class ToolError(Exception):
    """工具执行失败，消息会原样回喂给模型，让它自己纠正。"""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出工作目录下某个子目录的文件（返回相对路径）",
            "parameters": {
                "type": "object",
                "properties": {"dir": {"type": "string", "description": "相对路径，默认 ."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件的指定行区间",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "description": "起始行，从 1 开始"},
                    "end": {"type": "integer", "description": "结束行（含）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在工作目录内按正则搜索，返回匹配的文件与行号",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "description": "文件过滤，如 *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "把文件里唯一出现的 old 文本替换成 new（改代码用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "执行白名单内的只读命令并返回输出",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
]


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n...[已截断 {len(text) - limit} 字符]...\n{text[-tail:]}"


class Toolbox:
    """所有文件操作都被限制在 root 目录内。"""

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    # ---------- 安全边界 ----------
    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"路径越界：{rel} 不在工作目录 {self.root} 内")
        return p

    @staticmethod
    def _check_shell(cmd: str) -> list[str]:
        for bad in SHELL_FORBIDDEN_SUBSTR:
            if bad in cmd:
                raise ToolError(f"命令包含禁止的内容 {bad!r}，已拒绝执行")
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            raise ToolError(f"命令无法解析：{e}") from e
        if not argv:
            raise ToolError("命令为空")
        if argv[0] not in SHELL_WHITELIST:
            raise ToolError(f"命令 {argv[0]!r} 不在白名单内：{sorted(SHELL_WHITELIST)}")
        return argv

    # ---------- 工具实现 ----------
    def list_files(self, dir: str = ".") -> str:
        base = self._resolve(dir)
        if not base.is_dir():
            raise ToolError(f"{dir} 不是目录")
        rows = []
        for p in sorted(base.rglob("*")):
            if any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in p.parts):
                continue
            rel = p.relative_to(self.root).as_posix()
            if p.is_dir():
                continue
            rows.append(f"{rel}\t{p.stat().st_size}B")
        return _truncate("\n".join(rows) or "(空目录)")

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"文件不存在：{path}")
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            raise ToolError(f"读取失败：{e}") from e
        start = max(1, int(start))
        end = len(lines) if end is None else min(len(lines), int(end))
        if start > len(lines):
            raise ToolError(f"起始行 {start} 超过文件行数 {len(lines)}")
        chunk = [f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1)]
        return _truncate(f"(共 {len(lines)} 行，显示 {start}-{end})\n" + "\n".join(chunk))

    def search(self, pattern: str, glob: str = "*") -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"正则不合法：{e}") from e
        hits = []
        for p in sorted(self.root.rglob(glob)):
            if not p.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    rel = p.relative_to(self.root).as_posix()
                    hits.append(f"{rel}:{no}: {line.strip()[:160]}")
        return _truncate("\n".join(hits) or "(无匹配)")

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"文件不存在：{path}")
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ToolError("未找到要替换的文本，请先用 read_file 确认内容")
        if count > 1:
            raise ToolError(f"要替换的文本出现 {count} 次，不唯一，请给出更长的上下文")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"已修改 {path}（替换 1 处）"

    def run_shell(self, cmd: str, timeout: int = 20) -> str:
        argv = self._check_shell(cmd)
        try:
            proc = subprocess.run(
                argv, cwd=self.root, capture_output=True, text=True, timeout=timeout, shell=False
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"命令超时（>{timeout}s）：{cmd}")
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return _truncate(f"[exit={proc.returncode}]\n{out or '(无输出)'}")

    # ---------- 分发 ----------
    def call(self, name: str, arguments: dict) -> str:
        if name not in {s["function"]["name"] for s in TOOL_SCHEMAS}:
            raise ToolError(f"未知工具：{name}")
        fn = getattr(self, name)
        try:
            return fn(**arguments)
        except TypeError as e:
            raise ToolError(f"工具参数不合法：{e}") from e


def explain_error(name: str, err: Exception) -> str:
    """把异常转成给模型看的自然语言结果，让它自己纠正。"""
    return json.dumps({"tool": name, "ok": False, "error": str(err)}, ensure_ascii=False)
