from ibm_watsonx_orchestrate.agent_builder.tools import tool


@tool
def hello_tool(name: str) -> str:
    """Greet a user by name.

    Args:
        name: The name of the person to greet.

    Returns:
        A greeting message.
    """
    return f"Hello, {name}! Welcome to watsonx Orchestrate."
