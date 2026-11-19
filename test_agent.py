"""自检脚本：不联网、不需要 API key，验证工具层与 Agent 循环。

运行：python test_agent.py
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import build_context, run, truncate_tool_output
from llm import MockLLM
from tools import ToolError, Toolbox


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "b.py").write_text("print(1)\n", encoding="utf-8")
        self.box = Toolbox(str(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_and_read(self):
        out = self.box.list_files(".")
        self.assertIn("a.txt", out)
        self.assertIn("sub/b.py", out)
        self.assertIn("hello", self.box.read_file("a.txt"))
        self.assertIn("print(1)", self.box.read_file("sub/b.py"))

    def test_path_escape_rejected(self):
        with self.assertRaises(ToolError):
            self.box.read_file("../../etc/passwd")
        with self.assertRaises(ToolError):
            self.box.write_file("../escape.txt", "x") if hasattr(self.box, "write_file") else self.box.replace_in_file("../x", "a", "b")

    def test_replace_unique(self):
        msg = self.box.replace_in_file("a.txt", "world", "agent")
        self.assertIn("已修改", msg)
        self.assertEqual((self.tmp / "a.txt").read_text(encoding="utf-8"), "hello\nagent\n")

    def test_replace_not_unique_rejected(self):
        (self.tmp / "dup.txt").write_text("x\nx\n", encoding="utf-8")
        with self.assertRaises(ToolError) as cm:
            self.box.replace_in_file("dup.txt", "x", "y")
        self.assertIn("不唯一", str(cm.exception))

    def test_shell_whitelist(self):
        with self.assertRaises(ToolError):
            self.box.run_shell("rm -rf a.txt")
        with self.assertRaises(ToolError):
            self.box.run_shell("sudo ls")
        with self.assertRaises(ToolError):
            self.box.run_shell("ls; rm -rf a.txt")   # 串联注入必须被拦
        with self.assertRaises(ToolError):
            self.box.run_shell("cat a.txt | grep hello")

    def test_unknown_tool(self):
        with self.assertRaises(ToolError):
            self.box.call("delete_everything", {})


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "demo_app.py").write_text("a\nb\nc\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loop_executes_tools_then_finishes(self):
        script = [
            {"tool": "list_files", "args": {}},
            {"tool": "read_file", "args": {"path": "demo_app.py"}},
            {"tool": "run_shell", "args": {"cmd": "rm -rf demo_app.py"}},  # 应被拒绝但不崩
            {"final": "读到了 3 行。"},
        ]
        result = run(MockLLM(script), Toolbox(str(self.tmp)), "看下文件", verbose=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["final"], "读到了 3 行。")
        tools_used = [t["name"] for t in result["trace"] if t["type"] == "tool"]
        self.assertEqual(tools_used, ["list_files", "read_file", "run_shell"])
        # 危险命令被拒，文件仍在
        self.assertTrue((self.tmp / "demo_app.py").exists())
        rejected = [t for t in result["trace"] if t["type"] == "tool" and t["name"] == "run_shell"]
        self.assertFalse(rejected[0]["ok"])

    def test_max_turns_guard(self):
        script = [{"tool": "list_files", "args": {}} for _ in range(10)]
        result = run(MockLLM(script), Toolbox(str(self.tmp)), "死循环测试", verbose=False, max_turns=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["turns"], 3)

    def test_context_compaction(self):
        big = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
        big += [{"role": "tool", "content": "x" * 4000, "tool_call_id": str(i)} for i in range(10)]
        self.assertLessEqual(len(str(build_context(big))), 26000)

    def test_truncate(self):
        out = truncate_tool_output("y" * 5000, limit=100)
        self.assertIn("截断", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
