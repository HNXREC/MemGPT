import json
from typing import Any, Optional, Union

import humps
from composio.constants import DEFAULT_ENTITY_ID
from pydantic import BaseModel

from letta.constants import COMPOSIO_ENTITY_ENV_VAR_KEY, DEFAULT_MESSAGE_TOOL, DEFAULT_MESSAGE_TOOL_KWARG
from letta.schemas.enums import MessageRole
from letta.schemas.letta_message import AssistantMessage, ReasoningMessage, ToolCallMessage
from letta.schemas.letta_response import LettaResponse
from letta.schemas.message import MessageCreate


# TODO: This is kind of hacky, as this is used to search up the action later on composio's side
# TODO: So be very careful changing/removing these pair of functions
def generate_func_name_from_composio_action(action_name: str) -> str:
    """
    Generates the composio function name from the composio action.

    Args:
        action_name: The composio action name

    Returns:
        function name
    """
    return action_name.lower()


def generate_composio_action_from_func_name(func_name: str) -> str:
    """
    Generates the composio action from the composio function name.

    Args:
        func_name: The composio function name

    Returns:
        composio action name
    """
    return func_name.upper()


def generate_composio_tool_wrapper(action_name: str) -> tuple[str, str]:
    # Generate func name
    func_name = generate_func_name_from_composio_action(action_name)

    wrapper_function_str = f"""
def {func_name}(**kwargs):
    from composio_langchain import ComposioToolSet
    import os

    entity_id = os.getenv('{COMPOSIO_ENTITY_ENV_VAR_KEY}', '{DEFAULT_ENTITY_ID}')
    composio_toolset = ComposioToolSet(entity_id=entity_id)
    response = composio_toolset.execute_action(action='{action_name}', params=kwargs)

    if response["error"]:
        raise RuntimeError(response["error"])
    return response["data"]
    """

    # Compile safety check
    assert_code_gen_compilable(wrapper_function_str)

    return func_name, wrapper_function_str


def generate_langchain_tool_wrapper(
    tool: "LangChainBaseTool", additional_imports_module_attr_map: dict[str, str] = None
) -> tuple[str, str]:
    tool_name = tool.__class__.__name__
    import_statement = f"from langchain_community.tools import {tool_name}"
    extra_module_imports = generate_import_code(additional_imports_module_attr_map)

    # Safety check that user has passed in all required imports:
    assert_all_classes_are_imported(tool, additional_imports_module_attr_map)

    tool_instantiation = f"tool = {generate_imported_tool_instantiation_call_str(tool)}"
    run_call = f"return tool._run(**kwargs)"
    func_name = humps.decamelize(tool_name)

    # Combine all parts into the wrapper function
    wrapper_function_str = f"""
def {func_name}(**kwargs):
    import importlib
    {import_statement}
    {extra_module_imports}
    {tool_instantiation}
    {run_call}
"""

    # Compile safety check
    assert_code_gen_compilable(wrapper_function_str)

    return func_name, wrapper_function_str


def assert_code_gen_compilable(code_str):
    try:
        compile(code_str, "<string>", "exec")
    except SyntaxError as e:
        print(f"Syntax error in code: {e}")


def assert_all_classes_are_imported(tool: Union["LangChainBaseTool"], additional_imports_module_attr_map: dict[str, str]) -> None:
    # Safety check that user has passed in all required imports:
    tool_name = tool.__class__.__name__
    current_class_imports = {tool_name}
    if additional_imports_module_attr_map:
        current_class_imports.update(set(additional_imports_module_attr_map.values()))
    required_class_imports = set(find_required_class_names_for_import(tool))

    if not current_class_imports.issuperset(required_class_imports):
        err_msg = f"[ERROR] You are missing module_attr pairs in `additional_imports_module_attr_map`. Currently, you have imports for {current_class_imports}, but the required classes for import are {required_class_imports}"
        print(err_msg)
        raise RuntimeError(err_msg)


def find_required_class_names_for_import(obj: Union["LangChainBaseTool", BaseModel]) -> list[str]:
    """
    Finds all the class names for required imports when instantiating the `obj`.
    NOTE: This does not return the full import path, only the class name.

    We accomplish this by running BFS and deep searching all the BaseModel objects in the obj parameters.
    """
    class_names = {obj.__class__.__name__}
    queue = [obj]

    while queue:
        # Get the current object we are inspecting
        curr_obj = queue.pop()

        # Collect all possible candidates for BaseModel objects
        candidates = []
        if is_base_model(curr_obj):
            # If it is a base model, we get all the values of the object parameters
            # i.e., if obj('b' = <class A>), we would want to inspect <class A>
            fields = dict(curr_obj)
            # Generate code for each field, skipping empty or None values
            candidates = list(fields.values())
        elif isinstance(curr_obj, dict):
            # If it is a dictionary, we get all the values
            # i.e., if obj = {'a': 3, 'b': <class A>}, we would want to inspect <class A>
            candidates = list(curr_obj.values())
        elif isinstance(curr_obj, list):
            # If it is a list, we inspect all the items in the list
            # i.e., if obj = ['a', 3, None, <class A>], we would want to inspect <class A>
            candidates = curr_obj

        # Filter out all candidates that are not BaseModels
        # In the list example above, ['a', 3, None, <class A>], we want to filter out 'a', 3, and None
        candidates = filter(lambda x: is_base_model(x), candidates)

        # Classic BFS here
        for c in candidates:
            c_name = c.__class__.__name__
            if c_name not in class_names:
                class_names.add(c_name)
                queue.append(c)

    return list(class_names)


def generate_imported_tool_instantiation_call_str(obj: Any) -> Optional[str]:
    if isinstance(obj, (int, float, str, bool, type(None))):
        # This is the base case
        # If it is a basic Python type, we trivially return the string version of that value
        # Handle basic types
        return repr(obj)
    elif is_base_model(obj):
        # Otherwise, if it is a BaseModel
        # We want to pull out all the parameters, and reformat them into strings
        # e.g. {arg}={value}
        # The reason why this is recursive, is because the value can be another BaseModel that we need to stringify
        model_name = obj.__class__.__name__
        fields = obj.dict()
        # Generate code for each field, skipping empty or None values
        field_assignments = []
        for arg, value in fields.items():
            python_string = generate_imported_tool_instantiation_call_str(value)
            if python_string:
                field_assignments.append(f"{arg}={python_string}")

        assignments = ", ".join(field_assignments)
        return f"{model_name}({assignments})"
    elif isinstance(obj, dict):
        # Inspect each of the items in the dict and stringify them
        # This is important because the dictionary may contain other BaseModels
        dict_items = []
        for k, v in obj.items():
            python_string = generate_imported_tool_instantiation_call_str(v)
            if python_string:
                dict_items.append(f"{repr(k)}: {python_string}")

        joined_items = ", ".join(dict_items)
        return f"{{{joined_items}}}"
    elif isinstance(obj, list):
        # Inspect each of the items in the list and stringify them
        # This is important because the list may contain other BaseModels
        list_items = [generate_imported_tool_instantiation_call_str(v) for v in obj]
        filtered_list_items = list(filter(None, list_items))
        list_items = ", ".join(filtered_list_items)
        return f"[{list_items}]"
    else:
        # Otherwise, if it is none of the above, that usually means it is a custom Python class that is NOT a BaseModel
        # Thus, we cannot get enough information about it to stringify it
        # This may cause issues, but we are making the assumption that any of these custom Python types are handled correctly by the parent library, such as LangChain
        # An example would be that WikipediaAPIWrapper has an argument that is a wikipedia (pip install wikipedia) object
        # We cannot stringify this easily, but WikipediaAPIWrapper handles the setting of this parameter internally
        # This assumption seems fair to me, since usually they are external imports, and LangChain should be bundling those as module-level imports within the tool
        # We throw a warning here anyway and provide the class name
        print(
            f"[WARNING] Skipping parsing unknown class {obj.__class__.__name__} (does not inherit from the Pydantic BaseModel and is not a basic Python type)"
        )
        if obj.__class__.__name__ == "function":
            import inspect

            print(inspect.getsource(obj))

        return None


def is_base_model(obj: Any):
    from langchain_core.pydantic_v1 import BaseModel as LangChainBaseModel

    return isinstance(obj, BaseModel) or isinstance(obj, LangChainBaseModel)


def generate_import_code(module_attr_map: Optional[dict]):
    if not module_attr_map:
        return ""

    code_lines = []
    for module, attr in module_attr_map.items():
        module_name = module.split(".")[-1]
        code_lines.append(f"# Load the module\n    {module_name} = importlib.import_module('{module}')")
        code_lines.append(f"    # Access the {attr} from the module")
        code_lines.append(f"    {attr} = getattr({module_name}, '{attr}')")
    return "\n".join(code_lines)


def parse_letta_response_for_assistant_message(
    letta_response: LettaResponse,
    assistant_message_tool_name: str = DEFAULT_MESSAGE_TOOL,
    assistant_message_tool_kwarg: str = DEFAULT_MESSAGE_TOOL_KWARG,
) -> Optional[str]:
    reasoning_message = ""
    for m in letta_response.messages:
        if isinstance(m, AssistantMessage):
            return m.assistant_message
        elif isinstance(m, ToolCallMessage) and m.tool_call.name == assistant_message_tool_name:
            try:
                return json.loads(m.tool_call.arguments)[assistant_message_tool_kwarg]
            except Exception:  # TODO: Make this more specific
                continue
        elif isinstance(m, ReasoningMessage):
            # This is not ideal, but we would like to return something rather than nothing
            reasoning_message += f"{m.reasoning}\n"

    return None


import asyncio
from random import uniform
from typing import Optional


async def async_send_message_with_retries(
    server,
    sender_agent: "Agent",
    target_agent_id: str,
    message_text: str,
    max_retries: int,
    timeout: int,
    logging_prefix: Optional[str] = None,
) -> str:
    """
    Shared helper coroutine to send a message to an agent with retries and a timeout.

    Args:
        server: The Letta server instance (from get_letta_server()).
        sender_agent (Agent): The agent initiating the send action.
        target_agent_id (str): The ID of the agent to send the message to.
        message_text (str): The text to send as the user message.
        max_retries (int): Maximum number of retries for the request.
        timeout (int): Maximum time to wait for a response (in seconds).
        logging_prefix (str): A prefix to append to logging
    Returns:
        str: The response or an error message.
    """
    logging_prefix = logging_prefix or "[async_send_message_with_retries]"
    for attempt in range(1, max_retries + 1):
        try:
            messages = [MessageCreate(role=MessageRole.user, text=message_text, name=sender_agent.agent_state.name)]
            # Wrap in a timeout
            response = await asyncio.wait_for(
                server.send_message_to_agent(
                    agent_id=target_agent_id,
                    actor=sender_agent.user,
                    messages=messages,
                    stream_steps=False,
                    stream_tokens=False,
                    use_assistant_message=True,
                    assistant_message_tool_name=DEFAULT_MESSAGE_TOOL,
                    assistant_message_tool_kwarg=DEFAULT_MESSAGE_TOOL_KWARG,
                ),
                timeout=timeout,
            )

            # Extract assistant message
            assistant_message = parse_letta_response_for_assistant_message(
                response,
                assistant_message_tool_name=DEFAULT_MESSAGE_TOOL,
                assistant_message_tool_kwarg=DEFAULT_MESSAGE_TOOL_KWARG,
            )
            if assistant_message:
                msg = f"Agent {target_agent_id} said '{assistant_message}'"
                sender_agent.logger.info(f"{logging_prefix} - {msg}")
                return msg
            else:
                msg = f"(No response from agent {target_agent_id})"
                sender_agent.logger.info(f"{logging_prefix} - {msg}")
                return msg
        except asyncio.TimeoutError:
            error_msg = f"(Timeout on attempt {attempt}/{max_retries} for agent {target_agent_id})"
            sender_agent.logger.warning(f"{logging_prefix} - {error_msg}")
        except Exception as e:
            error_msg = f"(Error on attempt {attempt}/{max_retries} for agent {target_agent_id}: {e})"
            sender_agent.logger.warning(f"{logging_prefix} - {error_msg}")

        # Exponential backoff before retrying
        if attempt < max_retries:
            backoff = uniform(0.5, 2) * (2**attempt)
            sender_agent.logger.warning(f"{logging_prefix} - Retrying the agent to agent send_message...sleeping for {backoff}")
            await asyncio.sleep(backoff)
        else:
            sender_agent.logger.error(f"{logging_prefix} - Fatal error during agent to agent send_message: {error_msg}")
            return error_msg
