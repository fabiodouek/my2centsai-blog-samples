"""A minimal ADK agent whose generated Python runs in a Cloud Run sandbox (test 9).

Deploy with: ./t9_adk_deploy.sh  (adk deploy cloud_run --with_cloud_run_sandbox ...)
"""
import os

from google.adk.agents import LlmAgent
from google.adk.integrations.cloud_run import CloudRunSandboxCodeExecutor

INSTRUCTION = (
    "You have no tools and cannot call functions. To compute anything, reply with ONE markdown "
    "code block that starts with ```python and contains plain Python that prints its result. "
    "The code is run for you and its output comes back; then answer in one sentence. "
    "When asked to run exactly some code, put exactly that code in the block."
)

root_agent = LlmAgent(
    name="sbx_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3.8-flash"),
    instruction=os.environ.get("ADK_INSTRUCTION", INSTRUCTION),
    code_executor=CloudRunSandboxCodeExecutor(),  # timeout_seconds defaults to 300, no egress
)
