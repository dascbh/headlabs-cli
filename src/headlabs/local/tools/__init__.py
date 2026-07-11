"""Built-in tools for the `headlabs local` agent runtime."""
from headlabs.local.tools.base import BaseTool, ToolResult
from headlabs.local.tools.read_file import ReadFileTool
from headlabs.local.tools.edit_file import EditFileTool
from headlabs.local.tools.bash import BashTool
from headlabs.local.tools.execute_python import ExecutePythonTool
from headlabs.local.tools.web_search import WebSearchTool
from headlabs.local.tools.glob_tool import GlobTool
from headlabs.local.tools.grep_tool import GrepTool
from headlabs.local.tools.web_fetch import WebFetchTool
from headlabs.local.tools.todo_write import TodoWriteTool
from headlabs.local.tools.ask_user_question import AskUserQuestionTool
from headlabs.local.tools.config_tool import ConfigTool
from headlabs.local.tools.browser_devtools import BrowserDevtoolsTool

ALL_TOOLS: list[type[BaseTool]] = [
    ReadFileTool,
    EditFileTool,
    BashTool,
    ExecutePythonTool,
    WebSearchTool,
    GlobTool,
    GrepTool,
    WebFetchTool,
    TodoWriteTool,
    AskUserQuestionTool,
    ConfigTool,
    BrowserDevtoolsTool,
]

__all__ = [
    "BaseTool",
    "ToolResult",
    "ReadFileTool",
    "EditFileTool",
    "BashTool",
    "ExecutePythonTool",
    "WebSearchTool",
    "GlobTool",
    "GrepTool",
    "WebFetchTool",
    "TodoWriteTool",
    "AskUserQuestionTool",
    "ConfigTool",
    "BrowserDevtoolsTool",
    "ALL_TOOLS",
]
