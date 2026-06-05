from __future__ import annotations

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema


def basic_voice_tools() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="noop",
                description=(
                    "Intentionally do nothing and say nothing. Use this when the user is not addressing "
                    "Iris, the speech is ambient room conversation, or no spoken response is appropriate."
                ),
                properties={
                    "reason": {
                        "type": "string",
                        "enum": ["ambient", "not_addressed", "ambiguous", "already_handled"],
                        "description": "Why Iris should stay silent.",
                    },
                },
                required=["reason"],
            ),
            FunctionSchema(
                name="status",
                description="Get session and device status.",
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="command",
                description="Check background command status.",
                properties={
                    "requestId": {
                        "type": "string",
                        "description": "Optional command id. Omit for recent commands.",
                    },
                },
                required=[],
            ),
            FunctionSchema(
                name="shell_exec",
                description=(
                    "Run a safe, simple, one-line shell command on this Mac. Use this for direct local "
                    "commands that fit in one quick command invocation. Prefer this over the Codex agent for "
                    "fast checks or tiny actions such as date, pwd, ls, pgrep, open, mkdir, touch, or a single "
                    "script command. Use this for explicit requests to open a named Mac app with open -a. "
                    "Use the Codex agent instead for longer work, multi-step tasks, code edits, "
                    "debugging, investigation, or anything that needs interpretation over time. Ask the user "
                    "for permission before using this for sensitive commands. "
                    "Sensitive means likely to change or expose important local state, such as deleting, moving, "
                    "overwriting, installing, committing, pushing, killing processes, network calls "
                    "with credentials, or anything the user has not clearly authorized. Do not use shell operators, "
                    "chained commands, command substitution, or long-running interactive agents."
                ),
                properties={
                    "command": {
                        "type": "string",
                        "description": "Single shell command invocation. No pipes, redirects, semicolons, chaining, or command substitution.",
                    },
                    "timeoutSeconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Optional timeout in seconds.",
                    },
                },
                required=["command"],
            ),
            FunctionSchema(
                name="screen_vision",
                description=(
                    "Capture the Mac screen and let Gemini answer a visual question from the screenshot. "
                    "Use this when the user asks about what is visible, on screen, in an app window, in an "
                    "image, chart, page, UI, error dialog, or other visual state and the answer should come "
                    "from Gemini seeing the pixels directly. Do not use this for non-visual desktop actions "
                    "or multi-step computer work."
                ),
                properties={
                    "question": {
                        "type": "string",
                        "description": (
                            "The user's visual question rewritten clearly for Gemini. Include the target app, "
                            "window, or visual object if the user named one."
                        ),
                    },
                    "display": {
                        "type": "string",
                        "enum": ["main"],
                        "description": "Screen to capture. Currently only the main display is supported.",
                    },
                },
                required=["question"],
            ),
            FunctionSchema(
                name="camera_vision",
                description=(
                    "Capture one frame from the Mac camera and let Gemini answer a visual question from "
                    "that camera image. Use this when the user asks Iris to look through the camera, look "
                    "at them, identify something in front of the camera, inspect a physical object, or answer "
                    "from the room/camera view. Do not use this for screen contents; use screen_vision for that."
                ),
                properties={
                    "question": {
                        "type": "string",
                        "description": (
                            "The user's camera-view question rewritten clearly for Gemini. Include the physical "
                            "object, person, or scene if the user named one."
                        ),
                    },
                    "camera": {
                        "type": "string",
                        "enum": ["default"],
                        "description": "Camera to capture. Currently uses the default AVFoundation camera.",
                    },
                },
                required=["question"],
            ),
            FunctionSchema(
                name="end",
                description=(
                    "End the active voice chat. Use this for generic direct stop/cancel/end phrases "
                    "such as 'Iris stop', 'stop', 'cancel', 'that's all', or 'never mind' when the user "
                    "is not clearly referring to desktop/computer work."
                ),
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="volume",
                description="Change speaker volume.",
                properties={
                    "action": {
                        "type": "string",
                        "enum": ["set", "increase", "decrease", "mute", "unmute"],
                        "description": "Volume operation.",
                    },
                    "volume": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Target volume or change amount.",
                    },
                },
                required=["action"],
            ),
            FunctionSchema(
                name="light",
                description="Control status LEDs.",
                properties={
                    "effect": {
                        "type": "string",
                        "enum": ["off", "breath", "rainbow", "solid", "doa"],
                        "description": "LED effect.",
                    },
                    "color": {
                        "type": "string",
                        "description": "RGB color.",
                    },
                    "brightness": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 255,
                        "description": "LED brightness.",
                    },
                },
                required=[],
            ),
            FunctionSchema(
                name="discover",
                description="Scan local network, Wi-Fi, and Bluetooth.",
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="search",
                description="Search transcript history.",
                properties={
                    "query": {
                        "type": "string",
                        "description": "Search phrase.",
                    },
                    "from": {
                        "type": "string",
                        "description": "Inclusive ISO start time.",
                    },
                    "to": {
                        "type": "string",
                        "description": "Exclusive ISO end time.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Result limit.",
                    },
                },
                required=[],
            ),
            FunctionSchema(
                name="memory",
                description=(
                    "Manage Iris memories about the user. Use list to inspect, save for new stable facts/preferences, "
                    "update to correct stale memories, and delete to remove wrong or unwanted memories."
                ),
                properties={
                    "action": {
                        "type": "string",
                        "enum": ["list", "save", "update", "delete"],
                        "description": "Memory operation.",
                    },
                    "memoryId": {
                        "type": "string",
                        "description": "Required for update or delete. Use list first if the id is unknown.",
                    },
                    "content": {
                        "type": "string",
                        "description": "One concise memory. Required for save and usually for update.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "instruction"],
                        "description": "Memory type.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["explicit", "high", "medium"],
                        "description": "Use explicit when the user directly asked Iris to remember it.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum memories to list.",
                    },
                },
                required=["action"],
            ),
            FunctionSchema(
                name="agent",
                description=(
                    "Use Iris's local Mac desktop capability. Iris is already running inside the desktop app; "
                    "never tell the user to download or install Iris Desktop. Choose start for new computer "
                    "work, steer for instructions to the active desktop task, interrupt only to stop "
                    "desktop/computer work, and status to check the local desktop bridge. Do not use agent "
                    "when a regular Iris tool can fully handle the request. Use screen_vision instead when "
                    "Gemini should answer from the current screen pixels. Use camera_vision instead when "
                    "Gemini should answer from the current camera view. Use shell_exec instead for safe, "
                    "quick, one-line local commands with immediate results, including explicit requests to open "
                    "a named Mac app with open -a. Use agent for screen/app "
                    "inspection, active desktop-task steering, multi-step work, code edits, debugging, "
                    "investigation, current web/docs/pricing/news/benchmark research, or anything that "
                    "should be handled by Codex over time. Do not use this for generic voice stop/cancel/end "
                    "phrases; use the end tool instead."
                ),
                properties={
                    "agentId": {
                        "type": "string",
                        "description": "Optional Iris desktop agent id. Omit for the local desktop agent.",
                    },
                    "threadId": {
                        "type": "string",
                        "description": "Optional Codex thread id from Iris. Use it to continue or interrupt a specific desktop thread.",
                    },
                    "thread": {
                        "type": "string",
                        "enum": ["auto", "same", "new"],
                        "description": "Thread choice. auto continues this voice session, same uses the selected thread, new starts a fresh Codex thread.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["start", "steer", "interrupt", "status"],
                        "description": "Agent operation. Omit for a normal prompt.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Interpreted desktop task for Codex. Required for start or steer. Rewrite the user's "
                            "spoken request into a concrete actionable instruction; do not copy the raw transcript "
                            "unless the exact words are the task content."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional raw user wording, recent voice context, constraints, or ambiguity notes "
                            "that help Codex understand the interpreted task."
                        ),
                    },
                    "responseStyle": {
                        "type": "string",
                        "enum": ["brief", "normal", "detailed"],
                        "description": "How much detail the agent should include in its structured result.",
                    },
                    "delivery": {
                        "type": "string",
                        "enum": ["auto", "speak", "save", "silent"],
                        "description": "Preferred final-result handling. Use speak when the user asks to be told/notified, save for background or save-result requests, silent only when the user explicitly asks for no speech.",
                    },
                    "waitMs": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 35000,
                        "description": "Optional synchronous wait in milliseconds. Leave unset for normal start/steer so desktop work starts without blocking voice.",
                    },
                    "thinking": {
                        "type": "object",
                        "description": "Optional Codex reasoning controls for a new desktop turn. Leave unset for Iris's low-effort default; set it when the user asks for no thinking, faster work, or deeper reasoning.",
                        "properties": {
                            "effort": {
                                "type": "string",
                                "enum": ["none", "minimal", "low", "medium", "high"],
                                "description": "Reasoning effort for Codex. none means no reasoning when the selected Codex model supports it.",
                            },
                            "summary": {
                                "type": "string",
                                "enum": ["auto", "concise", "detailed", "none"],
                                "description": "Reasoning summary behavior for Codex.",
                            },
                        },
                    },
                },
                required=[],
            ),
        ]
    )
