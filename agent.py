"""迷你 Coding Agent —— 主循环。

用法：
    # 真实调用（需要 LLM_API_KEY）
    python agent.py --workspace ./demo --task "统计 demo 目录里每个 .py 文件的行数，汇报总数"

    # 离线演示（无需 key，走脚本化 mock）
    python agent.py --demo

    # 跑自检
    python test_agent.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from llm import LLM, MockLLM
from tools import TOOL_SCHEMAS, ToolError, Toolbox, explain_error

# Windows 控制台默认 GBK，强制 utf-8，避免打印中文/符号时崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MAX_TURNS = 12
MAX_CONTEXT_CHARS = 24000  # 粗略的上下文预算（字符数）


SYSTEM_PROMPT = """你是一个命令行编程助手，在用户的工作目录里完成任务。

规则：
1. 先看清楚再动手：用 list_files / search / read_file 了解现状，不要凭空假设。
2. 改代码用 replace_in_file，old 必须是文件里唯一出现的一段原文。
3. 执行命令只能用白名单内命令（ls/cat/grep/wc/find/git/python 等），不要试图串联。
4. 工具报错时，读错误信息，自己修正后重试，不要重复同样的调用。
5. 任务完成后用一两句话总结你做了什么、结果是什么。不要输出无关的解释。
"""


def build_context(messages: list[dict]) -> list[dict]:
    """上下文压缩：保留 system + 首条用户任务 + 最近若干轮，超出预算就丢中间。

    工业实现会做摘要，这里用「保留头尾」的朴素策略，取舍在 README 里写了。
    """
    if len(str(messages)) <= MAX_CONTEXT_CHARS:
        return messages
    head = messages[:2]                # system + 用户任务
    tail: list[dict] = []
    used = len(str(head))
    for m in reversed(messages[2:]):
        n = len(str(m))
        if used + n > MAX_CONTEXT_CHARS:
            break
        tail.append(m)
        used += n
    tail.reverse()
    note = {"role": "user", "content": "（中间的若干轮交互因上下文超限已省略）"}
    return head + [note] + tail


def truncate_tool_output(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[截断，原长 {len(text)}]"


def run(agent_llm, toolbox: Toolbox, task: str, verbose: bool = True, max_turns: int = MAX_TURNS) -> dict:
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    trace: list[dict] = []
    final = ""

    for turn in range(1, max_turns + 1):
        ctx = build_context(messages)
        t0 = time.time()
        resp = agent_llm.chat(ctx, TOOL_SCHEMAS)
        elapsed = time.time() - t0

        calls = resp.get("tool_calls") or []
        if resp.get("content"):
            final = resp["content"]

        if verbose:
            print(f"\n── 轮次 {turn}  ({elapsed:.2f}s) ──")
            if resp.get("content"):
                print(f"  思考: {resp['content'][:200]}")
            for c in calls:
                print(f"  调用: {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)[:160]})")

        if not calls:
            trace.append({"turn": turn, "type": "final", "content": final, "elapsed": elapsed})
            if verbose:
                print(f"\n[完成] {final}")
            return {"ok": True, "final": final, "turns": turn, "trace": trace}

        messages.append(
            {
                "role": "assistant",
                "content": resp.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in calls
                ],
            }
        )

        for c in calls:
            t1 = time.time()
            ok = True
            try:
                result = toolbox.call(c["name"], c["arguments"])
            except ToolError as e:
                ok = False
                result = explain_error(c["name"], e)
            except Exception as e:  # 兜底：绝不让工具异常把 Agent 打死
                ok = False
                result = explain_error(c["name"], RuntimeError(f"未预期异常：{e}"))

            cost = time.time() - t1
            trace.append(
                {
                    "turn": turn,
                    "type": "tool",
                    "name": c["name"],
                    "arguments": c["arguments"],
                    "ok": ok,
                    "result": result[:500],
                    "elapsed": round(cost, 3),
                }
            )
            if verbose:
                head = result.splitlines()[0][:120] if result else "(空)"
                print(f"    → {'ok' if ok else 'ERR'} {cost:.2f}s  {head}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": truncate_tool_output(result),
                }
            )

    return {"ok": False, "final": "达到最大轮数仍未完成", "turns": max_turns, "trace": trace}


def write_trace(result: dict, out_dir: str = ".") -> str:
    name = f"trace_{datetime.now():%Y%m%d_%H%M%S}.json"
    path = Path(out_dir) / name
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# 跨平台：不依赖 wc / ls 这类 Unix 专有命令
DEMO_SCRIPT = [
    {"think": "先看看目录里有什么", "tool": "list_files", "args": {"dir": "."}},
    {"think": "读一下主文件", "tool": "read_file", "args": {"path": "demo_app.py"}},
    {"think": "搜一下入口函数", "tool": "search", "args": {"pattern": "def \\w+", "glob": "*.py"}},
    {"think": "试一个危险命令，验证白名单拦截", "tool": "run_shell", "args": {"cmd": "rm -rf demo_app.py"}},
    {"final": "demo_app.py 一共 6 行，包含 main 入口；危险命令已被白名单拒绝，文件未被删除。"},
]


def make_demo_workspace() -> Path:
    root = Path("./demo_workspace").resolve()
    root.mkdir(exist_ok=True)
    (root / "demo_app.py").write_text(
        "def main():\n    print('hello agent')\n\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )
    (root / "readme.txt").write_text("这是一个演示工作目录\n", encoding="utf-8")
    return root


def main() -> int:
    ap = argparse.ArgumentParser(description="迷你 Coding Agent")
    ap.add_argument("--workspace", default=".", help="工作目录（所有文件操作限制在此）")
    ap.add_argument("--task", default=None, help="任务描述")
    ap.add_argument("--demo", action="store_true", help="离线演示（mock 模型）")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.demo:
        root = make_demo_workspace()
        agent_llm = MockLLM(DEMO_SCRIPT)
        task = "统计 demo_workspace 里 Python 文件的总行数"
    else:
        root = Path(args.workspace).resolve()
        if not root.is_dir():
            print(f"工作目录不存在：{root}", file=sys.stderr)
            return 2
        agent_llm = LLM()
        if not agent_llm.online:
            print("未设置 LLM_API_KEY。可以用 --demo 跑离线演示：", file=sys.stderr)
            print("  python agent.py --demo", file=sys.stderr)
            return 2
        if not args.task:
            print("缺少 --task", file=sys.stderr)
            return 2
        task = args.task

    toolbox = Toolbox(str(root))
    result = run(agent_llm, toolbox, task, verbose=not args.quiet, max_turns=args.max_turns)
    path = write_trace(result)
    print(f"\n[trace] 已保存：{path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
